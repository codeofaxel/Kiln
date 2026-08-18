"""mark_geometry — the 2D mark compiler (SVG parse + raster trace).

Regression backstops for the logo-deboss engine failure modes:

* ``<path>``-based SVGs dropped entirely (regex parser saw only
  polygon/rect/circle) → mis-placed ``import()`` fallback.
* Offset-origin viewBoxes placing the mark off the face.
* Raster logos carved as whole-tile heightmaps: background carve +
  perimeter frame + pixel staircase + vertically mirrored content.
* ``fill()`` erasing even-odd holes (letter counters, outline bands).

If any of these regress, a user's logo renders as the amateur artifact
set again — these tests are the structural proof they cannot.
"""

import logging
import math
import os

import pytest

from kiln.mark_geometry import (
    MarkGeometry,
    _dash_runs,
    _parse_dasharray,
    _ring_area,
    _stroke_segments_to_rings,
    is_bilevel_image,
    parse_svg_to_mark,
    trace_image_to_mark,
)

# A mark shaped like the failure case that motivated the parser: pure
# <path> geometry, offset-origin viewBox, even-odd subpath holes.
# (Synthetic — same structure as a real brand mark, no brand data.)
OFFSET_VIEWBOX_SVG = """<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg width="102mm" height="85mm" viewBox="-51 -45 102 85" xmlns="http://www.w3.org/2000/svg">
<path d="M -40,-40 L 40,-40 L 40,30 L -40,30 z M -30,-30 L 30,-30 L 30,20 L -30,20 z"
      stroke="black" stroke-width="0.35" fill="black" fill-rule="evenodd"/>
</svg>
"""


class TestSvgParser:
    def test_offset_viewbox_path_parses_and_centers(self):
        m = parse_svg_to_mark(OFFSET_VIEWBOX_SVG)
        assert m is not None and not m.is_empty
        # One element → one even-odd group with outer ring + hole ring.
        assert len(m.groups) == 1
        assert len(m.groups[0]) == 2
        # Exact content bounds (80 x 70 band), centered on the origin.
        assert m.width == pytest.approx(80.0)
        assert m.height == pytest.approx(70.0)
        b = m.content_bounds_info()
        assert b["content_x_min"] == pytest.approx(-40.0)
        assert b["content_y_min"] == pytest.approx(-35.0)
        xs = [x for ring in m.groups[0] for x, _ in ring]
        ys = [y for ring in m.groups[0] for _, y in ring]
        assert max(xs) == pytest.approx(-min(xs))
        assert max(ys) == pytest.approx(-min(ys))

    def test_holes_survive_to_scad_as_evenodd_paths(self):
        m = parse_svg_to_mark(OFFSET_VIEWBOX_SVG)
        scad = m.to_scad()
        # ONE polygon() with TWO paths = outer + hole under even-odd.
        assert scad.count("polygon(") == 1
        assert scad.count("paths=[[") == 1
        assert scad.count("],[") >= 1  # second path present

    def test_to_scad_grows_union_to_dissolve_tangencies(self):
        # Sub-polygons that touch edge-on without overlapping (a glyph
        # stem meeting its diagonal) extrude into pinched, non-manifold
        # edges unless the evaluated region is grown by an epsilon so the
        # tangency becomes a real overlap.  The wrap must GROW only —
        # a grow-then-shrink closing re-creates the near-tangency.
        from kiln.mark_geometry import MarkGeometry

        tangent = MarkGeometry(
            groups=[
                [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]],
                # Second square shares the x=10 edge exactly: tangent contact.
                [[(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)]],
            ],
            width=20.0,
            height=10.0,
        )
        scad = tangent.to_scad()
        assert scad.startswith("offset(delta=")
        # Epsilon scales with the mark: 0.05% of the largest dimension.
        assert "offset(delta=0.0100)" in scad
        # Grow only — no matching negative offset shrinking back.
        assert "offset(delta=-" not in scad

    def test_to_scad_empty_mark_emits_nothing(self):
        from kiln.mark_geometry import MarkGeometry

        assert MarkGeometry().to_scad() == ""

    def test_curves_flatten_and_land_on_endpoints(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10,50 C 10,10 90,10 90,50 Q 50,90 10,50 z" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        ring = m.groups[0][0]
        assert len(ring) > 20  # curves actually flattened, not chorded
        assert m.width == pytest.approx(80.0, abs=1.0)

    def test_arc_command(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 20,50 A 30,30 0 1 1 80,50 A 30,30 0 1 1 20,50 z" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        # Full circle of radius 30 → 60 x 60 bbox.
        assert m.width == pytest.approx(60.0, abs=0.5)
        assert m.height == pytest.approx(60.0, abs=0.5)

    def test_transforms_compose(self):
        svg = (
            '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">'
            '<g transform="translate(50,50) scale(2)">'
            '<rect x="0" y="0" width="10" height="5" fill="black"/></g></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(20.0)
        assert m.height == pytest.approx(10.0)

    def test_rotate_transform(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0" y="0" width="10" height="10" transform="rotate(45)" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(10 * math.sqrt(2), abs=0.1)

    def test_white_fill_is_background(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
            '<rect x="10" y="10" width="20" height="20" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(20.0)  # only the ink rect

    def test_stroke_only_line_expands_to_quads(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<line x1="10" y1="10" x2="90" y2="10" stroke="black" stroke-width="4"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        # SVG's default linecap is butt: the stroke stops dead at each
        # endpoint, so the band is exactly the 80-unit span, not 80 + caps.
        assert m.width == pytest.approx(80.0, abs=0.01)
        assert m.height == pytest.approx(4.0, abs=0.5)

    @pytest.mark.parametrize(
        ("cap", "expected_width"),
        [("", 80.0), ('stroke-linecap="butt"', 80.0),
         ('stroke-linecap="round"', 84.0), ('stroke-linecap="square"', 84.0)],
    )
    def test_linecap_controls_endpoint_overshoot(self, cap, expected_width):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="10" y1="10" x2="90" y2="10" stroke="black" stroke-width="4" {cap}/>'
            "</svg>"
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(expected_width, abs=0.01)
        assert m.height == pytest.approx(4.0, abs=0.01)

    def test_linecap_inherits_from_group(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<g stroke-linecap="round">'
            '<line x1="10" y1="10" x2="90" y2="10" stroke="black" stroke-width="4"/>'
            "</g></svg>"
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(84.0, abs=0.01)  # round cap inherited

    def test_unknown_linecap_falls_back_to_butt(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<line x1="10" y1="10" x2="90" y2="10" stroke="black"'
            ' stroke-width="4" stroke-linecap="bogus"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(80.0, abs=0.01)

    def test_closed_stroke_has_no_caps(self):
        # Every point on a closed subpath is a joint, so linecap is
        # irrelevant — the corner plugs must survive regardless.
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10 10 L 90 10 L 50 90 z" fill="none" stroke="black"'
            ' stroke-width="4" stroke-linecap="butt"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert len(m.groups) == 6  # 3 segment quads + one join per corner

    def test_open_stroked_path_stays_open(self):
        # Five-segment open path (no Z) — the top gap is deliberate.
        # Sealing it draws a stroke-width band straight across the gap.
        svg = (
            '<svg viewBox="0 0 1024 640" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 418.4 60 L 252 60 L 96 580 L 928 580 L 772 60 L 605.6 60"'
            ' fill="none" stroke="black" stroke-width="20.15"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        # 6 points → 5 stroke quads (NOT 6) + 4 interior joins.  The 2
        # endpoints are butt caps, so they contribute no geometry at all.
        assert len(m.groups) == 9
        # Nothing spans the top gap between x=418.4 and x=605.6 at y=60.
        gap_lo, gap_hi = 418.4 - 512, 605.6 - 512  # recentered frame
        for g in m.groups:
            xs = sorted(x for x, _ in g[0])
            assert not (
                xs[0] == pytest.approx(gap_lo, abs=1.0)
                and xs[-1] == pytest.approx(gap_hi, abs=1.0)
            )

    def test_stroked_path_with_z_still_closes(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10 10 L 90 10 L 50 90 z"'
            ' fill="none" stroke="black" stroke-width="4"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        # Explicit Z: 3 points → 3 quads (closing segment kept) + 3 joins.
        assert len(m.groups) == 6

    def test_open_two_point_stroked_path(self):
        # A path equivalent to <line> must stroke as a single segment.
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10 10 L 90 10" fill="none" stroke="black" stroke-width="4"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert m.width == pytest.approx(80.0, abs=0.01)
        assert m.height == pytest.approx(4.0, abs=0.5)

    def test_miter_join_produces_the_exact_outer_corner(self):
        # An L with a 90° turn: the miter tip must land on the true outer
        # corner, so the stroked band reads as a square corner, not a nub.
        rings = _stroke_segments_to_rings(
            [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)], 20.0, False, "butt", "miter"
        )
        tips = [pt for ring in rings for pt in ring]
        assert any(
            x == pytest.approx(110.0) and y == pytest.approx(-10.0) for x, y in tips
        )

    @pytest.mark.parametrize(
        ("join", "miterlimit", "expected_min_y"),
        [
            # Interior angle 53.13° → miter ratio 1/sin(θ/2) = 2.236, so the
            # spike reaches 2.236 × half-width past the vertex.
            ("miter", 4.0, -22.361),
            ("miter", 2.0, -4.472),  # ratio over the limit → bevels instead
            ("round", 4.0, -10.000),  # arc of exactly one half-width
            ("bevel", 4.0, -4.472),  # flat chord between the outer corners
        ],
    )
    def test_join_shape_matches_svg_geometry(self, join, miterlimit, expected_min_y):
        rings = _stroke_segments_to_rings(
            [(0.0, 200.0), (100.0, 0.0), (200.0, 200.0)],
            20.0, False, "butt", join, miterlimit,
        )
        min_y = min(y for ring in rings for _, y in ring)
        assert min_y == pytest.approx(expected_min_y, abs=0.001)

    def test_miter_is_the_default_join(self):
        pts = [(0.0, 200.0), (100.0, 0.0), (200.0, 200.0)]
        default = _stroke_segments_to_rings(pts, 20.0, False)
        explicit = _stroke_segments_to_rings(pts, 20.0, False, "butt", "miter", 4.0)
        assert default == explicit

    def test_linejoin_and_miterlimit_inherit_from_group(self):
        svg = (
            '<svg viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg">'
            '<g stroke-linejoin="bevel">'
            '<polyline points="0,200 100,0 200,200" fill="none"'
            ' stroke="black" stroke-width="20"/></g></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        # Bevel keeps the corner within one half-width of the apex; the
        # default miter would spike 2.236 half-widths and stand taller.
        assert m.height == pytest.approx(208.944, abs=0.01)
        assert parse_svg_to_mark(
            svg.replace('<g stroke-linejoin="bevel">', "<g>")
        ).height == pytest.approx(226.833, abs=0.01)

    def test_over_limit_miter_falls_back_to_bevel(self):
        svg = (
            '<svg viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg">'
            '<polyline points="0,200 100,0 200,200" fill="none" stroke="black"'
            ' stroke-width="20" stroke-miterlimit="{}"/></svg>'
        )
        # Ratio here is 2.236: a limit below it bevels, above it miters.
        assert parse_svg_to_mark(svg.format(2)).height == pytest.approx(208.944, abs=0.01)
        assert parse_svg_to_mark(svg.format(10)).height == pytest.approx(226.833, abs=0.01)

    def test_collinear_points_need_no_join(self):
        # Straight-through joints: the segment quads already abut, so a
        # join ring there would be pure waste in the OpenSCAD output.
        rings = _stroke_segments_to_rings(
            [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)], 10.0, False
        )
        assert len(rings) == 2  # two segment quads, no join

    @pytest.mark.parametrize("join", ["miter", "round", "bevel"])
    @pytest.mark.parametrize("cap", ["butt", "round", "square"])
    @pytest.mark.parametrize(
        ("pts", "closed"),
        [
            ([(0.0, 0.0), (0.0, 0.0), (50.0, 0.0)], False),  # duplicate points
            ([(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)], True),  # all identical
            ([(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 0.0)], True),  # restates start
            ([(0.0, 0.0), (50.0, 0.0)], True),  # too few points to close
            ([(0.0, 0.0), (50.0, 0.0), (0.0, 0.0)], False),  # 180° cusp
        ],
    )
    def test_degenerate_strokes_emit_no_junk(self, pts, closed, cap, join):
        for ring in _stroke_segments_to_rings(pts, 10.0, closed, cap, join):
            assert len(ring) >= 3
            assert abs(_ring_area(ring)) > 1e-12
            assert all(math.isfinite(v) for pt in ring for v in pt)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("10", [10.0, 10.0]),  # odd list repeats into dash/gap pairs
            ("10 5 2", [10.0, 5.0, 2.0, 10.0, 5.0, 2.0]),
            ("10,5", [10.0, 5.0]),
            ("none", None),
            ("5 -3", None),  # negative is invalid → solid, not blank
            ("0 0", None),  # nothing to draw with
            ("10%", None),  # viewport-relative; not tracked, so solid
            ("", None),
        ],
    )
    def test_dasharray_parsing(self, value, expected):
        assert _parse_dasharray(value) == expected

    def test_dashes_ink_the_on_fraction_of_the_path(self):
        line = [(0.0, 0.0), (100.0, 0.0)]
        runs = _dash_runs(line, False, [10.0, 10.0], 0.0)
        assert [(r[0][0], r[-1][0]) for r in runs] == [
            (0.0, 10.0), (20.0, 30.0), (40.0, 50.0), (60.0, 70.0), (80.0, 90.0)
        ]

    @pytest.mark.parametrize(
        ("offset", "first_run"),
        [
            (0.0, (0.0, 10.0)),
            (5.0, (0.0, 5.0)),  # starts halfway through a dash
            (-5.0, (5.0, 15.0)),  # starts inside a gap
            (10.0, (10.0, 20.0)),  # a whole dash into the pattern
        ],
    )
    def test_dashoffset_shifts_the_pattern(self, offset, first_run):
        runs = _dash_runs([(0.0, 0.0), (100.0, 0.0)], False, [10.0, 10.0], offset)
        assert (runs[0][0][0], runs[0][-1][0]) == pytest.approx(first_run)

    def test_dash_spanning_a_corner_keeps_the_vertex(self):
        # Otherwise the dash would cut the corner as one straight chord and
        # lose its join entirely.
        runs = _dash_runs(
            [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)], False, [100.0, 10.0], 0.0
        )
        assert runs[0] == [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)]

    def test_closed_path_dashes_around_the_whole_perimeter(self):
        square = [(0.0, 0.0), (60.0, 0.0), (60.0, 60.0), (0.0, 60.0)]
        runs = _dash_runs(square, True, [30.0, 30.0], 0.0)
        inked = sum(
            math.hypot(r[i + 1][0] - r[i][0], r[i + 1][1] - r[i][1])
            for r in runs
            for i in range(len(r) - 1)
        )
        assert inked == pytest.approx(120.0)  # half of the 240 perimeter

    def test_runaway_dasharray_falls_back_to_solid_and_says_so(self, caplog):
        line = [(0.0, 0.0), (100000.0, 0.0)]
        assert _dash_runs(line, False, [0.01, 0.01], 0.0) is None
        with caplog.at_level(logging.WARNING, logger="kiln.mark_geometry"):
            rings = _stroke_segments_to_rings(
                line, 4.0, False, "butt", "miter", 4.0, [0.01, 0.01], 0.0
            )
        assert len(rings) == 1  # one solid quad, not a million dashes
        assert "solid" in caplog.text

    def test_dasharray_reaches_the_mark_through_svg(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<line x1="0" y1="10" x2="100" y2="10" stroke="black"'
            ' stroke-width="4" stroke-dasharray="10 10"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        assert m is not None
        assert len(m.groups) == 5  # five 10-unit dashes across 100 units

    def test_dasharray_inherits_from_group(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<g stroke-dasharray="10 10">'
            '<line x1="0" y1="10" x2="100" y2="10" stroke="black" stroke-width="4"/>'
            "</g></svg>"
        )
        assert len(parse_svg_to_mark(svg).groups) == 5

    def test_dashed_stroke_still_caps_and_joins_each_dash(self):
        # Square caps extend every dash by half a width at both ends, so a
        # 10-unit dash inks 14 — proof the cap logic runs per dash.
        line = [(0.0, 0.0), (100.0, 0.0)]
        butt = _stroke_segments_to_rings(
            line, 4.0, False, "butt", "miter", 4.0, [10.0, 10.0], 0.0
        )
        square = _stroke_segments_to_rings(
            line, 4.0, False, "square", "miter", 4.0, [10.0, 10.0], 0.0
        )
        assert len(butt) == 5  # one quad per dash
        assert len(square) == 15  # plus two cap quads per dash

    @pytest.mark.parametrize(
        ("cap", "draws"), [("round", True), ("square", True), ("butt", False)]
    )
    def test_zero_length_dash_is_a_dot_not_a_disappearance(self, cap, draws):
        # "0 12" with round caps is THE SVG dotted-line idiom.  Treating a
        # zero-length dash as nothing to draw silently erases the mark.
        svg = (
            '<svg viewBox="0 0 220 20" xmlns="http://www.w3.org/2000/svg">'
            '<line x1="10" y1="10" x2="210" y2="10" stroke="black"'
            f' stroke-width="7" stroke-dasharray="0 12" stroke-linecap="{cap}"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        if not draws:
            assert m is None  # butt caps on zero-length dashes render nothing
            return
        assert m is not None
        assert len(m.groups) == 34  # 17 dots, two half-caps apiece
        assert m.height == pytest.approx(7.0, abs=0.01)  # one stroke width across

    @pytest.mark.parametrize("pattern", [[10.0, 10.0], [3.0, 7.0], [25.0, 5.0]])
    @pytest.mark.parametrize("offset", [0.0, 5.0, -13.0, 100.0])
    @pytest.mark.parametrize("closed", [True, False])
    def test_dashed_output_is_always_well_formed(self, pattern, offset, closed):
        rings = _stroke_segments_to_rings(
            [(0.0, 0.0), (60.0, 0.0), (60.0, 60.0), (0.0, 60.0)],
            6.0, closed, "round", "miter", 4.0, pattern, offset,
        )
        assert rings
        for ring in rings:
            assert len(ring) >= 3
            assert abs(_ring_area(ring)) > 1e-12
            assert all(math.isfinite(v) for pt in ring for v in pt)

    @pytest.mark.parametrize(
        ("markup", "draws"),
        [
            ('stroke="black" stroke-width="4"', True),
            ('stroke="black" stroke-width="4" stroke-opacity="0"', False),
            ('stroke="black" stroke-width="4" opacity="0"', False),
            ('stroke="black" stroke-width="4" style="stroke-opacity:0"', False),
            # Carved geometry is binary — partial opacity still has to cut.
            ('stroke="black" stroke-width="4" stroke-opacity="0.5"', True),
        ],
    )
    def test_transparent_strokes_are_not_drawn(self, markup, draws):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="10" y1="10" x2="90" y2="10" {markup}/></svg>'
        )
        assert (parse_svg_to_mark(svg) is not None) is draws

    def test_stroke_opacity_inherits_from_group(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<g stroke-opacity="0">'
            '<line x1="10" y1="10" x2="90" y2="10" stroke="black" stroke-width="4"/>'
            "</g></svg>"
        )
        assert parse_svg_to_mark(svg) is None

    @staticmethod
    def _stroke_thickness(inner):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            f"{inner}</svg>"
        )
        quad = next(g[0] for g in parse_svg_to_mark(svg).groups if len(g[0]) == 4)
        return min(
            math.hypot(quad[(i + 1) % 4][0] - quad[i][0], quad[(i + 1) % 4][1] - quad[i][1])
            for i in range(4)
        )

    @pytest.mark.parametrize(
        ("transform", "plain", "non_scaling"),
        [
            ("", 1.0, 1.0),
            ('transform="scale(4)"', 4.0, 1.0),
            ('transform="rotate(30)"', 1.0, 1.0),
            ('transform="rotate(30) scale(4)"', 4.0, 1.0),
            ('transform="translate(9,3) scale(2.5)"', 2.5, 1.0),
            # One scalar width cannot be right in both axes under an
            # anisotropic scale; sqrt|det| lands on the geometric mean.
            ('transform="scale(2,8)"', 8.0, 2.0),
        ],
    )
    def test_non_scaling_stroke_survives_ancestor_transforms(
        self, transform, plain, non_scaling
    ):
        line = (
            '<line x1="1" y1="5" x2="20" y2="5" stroke="black" stroke-width="1" {}/>'
        )
        effect = 'vector-effect="non-scaling-stroke"'
        assert self._stroke_thickness(
            f"<g {transform}>{line.format('')}</g>"
        ) == pytest.approx(plain, abs=0.001)
        assert self._stroke_thickness(
            f"<g {transform}>{line.format(effect)}</g>"
        ) == pytest.approx(non_scaling, abs=0.001)

    def test_non_scaling_stroke_reads_from_style_attribute(self):
        line = (
            '<line x1="1" y1="5" x2="20" y2="5" stroke="black" stroke-width="1"'
            ' style="vector-effect:non-scaling-stroke"/>'
        )
        assert self._stroke_thickness(
            f'<g transform="scale(4)">{line}</g>'
        ) == pytest.approx(1.0, abs=0.001)

    def test_non_scaling_stroke_does_not_inherit(self):
        # SVG marks vector-effect as Inherited: no, so putting it on a parent
        # must not silently thin every stroke beneath it.
        line = '<line x1="1" y1="5" x2="20" y2="5" stroke="black" stroke-width="1"/>'
        assert self._stroke_thickness(
            f'<g transform="scale(4)" vector-effect="non-scaling-stroke">{line}</g>'
        ) == pytest.approx(4.0, abs=0.001)

    def test_min_stroke_floor_outranks_non_scaling_shrink(self):
        # The floor exists so a hairline still prints; it must not be
        # undercut by dividing the width back down.
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<g transform="scale(10)"><line x1="1" y1="5" x2="20" y2="5"'
            ' stroke="black" stroke-width="1"'
            ' vector-effect="non-scaling-stroke"/></g></svg>'
        )
        m = parse_svg_to_mark(svg, min_stroke_units=0.5)
        quad = next(g[0] for g in m.groups if len(g[0]) == 4)
        thickness = min(
            math.hypot(quad[(i + 1) % 4][0] - quad[i][0], quad[(i + 1) % 4][1] - quad[i][1])
            for i in range(4)
        )
        assert thickness == pytest.approx(5.0, abs=0.001)  # floor 0.5 × scale 10

    def test_y_axis_flip_top_stays_top(self):
        # Ink only in the TOP half of the SVG (small y).  After compile
        # (Y-up frame), that geometry must sit at POSITIVE y.
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<rect x="40" y="0" width="20" height="10" fill="black"/>'
            '<rect x="40" y="80" width="20" height="20" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)
        top_rect_group = m.groups[0]  # first element = the y=0 rect
        ys = [y for ring in top_rect_group for _, y in ring]
        assert min(ys) > 0  # SVG-top geometry is math-top geometry

    def test_text_only_svg_returns_none(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            "<text x=\"10\" y=\"50\">KILN</text></svg>"
        )
        assert parse_svg_to_mark(svg) is None

    def test_entity_declarations_rejected(self):
        evil = (
            '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]>'
            '<svg viewBox="0 0 10 10"><rect width="5" height="5" fill="black"/></svg>'
        )
        assert parse_svg_to_mark(evil) is None

    def test_malformed_path_does_not_crash(self):
        svg = (
            '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M 10,10 L 90" fill="black"/>'
            '<rect x="10" y="10" width="30" height="30" fill="black"/></svg>'
        )
        m = parse_svg_to_mark(svg)  # bad path skipped, rect survives
        assert m is not None
        assert m.width == pytest.approx(30.0)


def _write_png(tmp_path, name, draw_fn, size=(200, 100), mode="L", bg=255):
    from PIL import Image, ImageDraw

    img = Image.new(mode, size, bg)
    draw_fn(ImageDraw.Draw(img))
    p = os.path.join(str(tmp_path), name)
    img.save(p)
    return p


class TestRasterTrace:
    def test_rect_traces_to_crisp_ring(self, tmp_path):
        p = _write_png(tmp_path, "rect.png", lambda d: d.rectangle([30, 20, 79, 59], fill=0))
        m = trace_image_to_mark(p)
        assert m is not None
        assert len(m.groups) == 1 and len(m.groups[0]) == 1
        ring = m.groups[0][0]
        assert len(ring) <= 8  # staircase-free rectangle collapses to corners
        assert m.width == pytest.approx(50.0, abs=1.5)
        assert m.height == pytest.approx(40.0, abs=1.5)

    def test_donut_keeps_hole(self, tmp_path):
        def _draw(d):
            d.ellipse([20, 10, 100, 90], fill=0)
            d.ellipse([40, 30, 80, 70], fill=255)

        p = _write_png(tmp_path, "donut.png", _draw, size=(120, 100))
        m = trace_image_to_mark(p)
        assert m is not None
        assert len(m.groups[0]) == 2  # outer + hole
        areas = sorted(abs(_ring_area(r)) for r in m.groups[0])
        assert areas[0] < areas[1]  # hole strictly inside outer

    def test_no_frame_no_background(self, tmp_path):
        # THE framelessness regression: geometry must exist ONLY where
        # ink is — nothing at the image border, no full-tile ring.
        p = _write_png(
            tmp_path, "mark.png", lambda d: d.rectangle([80, 40, 119, 59], fill=0)
        )
        m = trace_image_to_mark(p)
        assert m is not None
        # Content bbox = the ink bbox (40x20), NOT the image tile (200x100).
        assert m.width == pytest.approx(40.0, abs=1.5)
        assert m.height == pytest.approx(20.0, abs=1.5)

    def test_orientation_not_mirrored(self, tmp_path):
        # Ink square in the image's TOP-LEFT corner region.  After
        # compile (centered, Y-up), it must sit at negative-x POSITIVE-y.
        # The old stencil heightmap rendered this upside down.
        p = _write_png(
            tmp_path, "corner.png", lambda d: d.rectangle([10, 10, 40, 30], fill=0),
        )
        m = trace_image_to_mark(p)
        assert m is not None
        # Add a second anchor so the mark isn't recentred onto itself.
        p2 = _write_png(
            tmp_path,
            "corner2.png",
            lambda d: (
                d.rectangle([10, 10, 40, 30], fill=0),
                d.rectangle([160, 80, 190, 95], fill=0),
            ),
        )
        m2 = trace_image_to_mark(p2)
        rings = m2.groups[0]
        # The top-left ink ring: most-negative x centroid.
        def _centroid(ring):
            return (
                sum(x for x, _ in ring) / len(ring),
                sum(y for _, y in ring) / len(ring),
            )

        left_ring = min(rings, key=lambda r: _centroid(r)[0])
        cx, cy = _centroid(left_ring)
        assert cx < 0 and cy > 0  # image-top-left → math-top-left

    def test_transparent_background_is_ground_not_ink(self, tmp_path):
        # RGBA logo with transparent background: the old pipeline's
        # bare convert('L') turned transparency into solid black ink.
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle([30, 30, 69, 69], fill=(0, 0, 0, 255))
        p = os.path.join(str(tmp_path), "alpha.png")
        img.save(p)
        m = trace_image_to_mark(p)
        assert m is not None
        assert m.width == pytest.approx(40.0, abs=1.5)  # the square, not the tile

    def test_blank_image_returns_none(self, tmp_path):
        p = _write_png(tmp_path, "blank.png", lambda d: None)
        assert trace_image_to_mark(p) is None


class TestBilevelDetector:
    def test_logo_detected(self, tmp_path):
        p = _write_png(
            tmp_path, "logo.png", lambda d: d.rectangle([20, 20, 120, 60], fill=0)
        )
        assert is_bilevel_image(p) is True

    def test_gradient_photo_rejected(self, tmp_path):
        from PIL import Image

        img = Image.new("L", (128, 128))
        img.putdata([(x + y) % 256 for y in range(128) for x in range(128)])
        p = os.path.join(str(tmp_path), "grad.png")
        img.save(p)
        assert is_bilevel_image(p) is False


class TestEmbossIntegration:
    def test_prepare_svg_uses_parser_and_centers(self, tmp_path):
        from kiln.image_to_surface import prepare_svg_for_emboss

        svg_p = os.path.join(str(tmp_path), "mark.svg")
        with open(svg_p, "w") as f:
            f.write(OFFSET_VIEWBOX_SVG)
        info = prepare_svg_for_emboss(svg_p, str(tmp_path))
        # Tangency-dissolving grow wrap, then the union of polygon groups.
        assert info["openscad_polygons"].startswith("offset(delta=")
        assert "union()" in info["openscad_polygons"]
        assert info["openscad_polygons_fill_safe"] is False
        # Centered content bounds — the placement translate becomes 0,0.
        assert info["content_x_min"] == pytest.approx(-info["content_width"] / 2)
        assert info["content_y_min"] == pytest.approx(-info["content_height"] / 2)

    def test_prepare_logo_image_returns_boolean_contract(self, tmp_path):
        from kiln.image_to_surface import prepare_logo_image_for_emboss

        p = _write_png(
            tmp_path, "logo.png", lambda d: d.rectangle([30, 20, 79, 59], fill=0)
        )
        info = prepare_logo_image_for_emboss(p, str(tmp_path))
        assert info["type"] == "svg"  # boolean-carve path, not heightmap
        assert "polygon(" in info["openscad_polygons"]
        assert info["traced_from_raster"] is True
        assert info["content_x_min"] == pytest.approx(-info["content_width"] / 2)

    def test_generated_scad_carves_polygons_not_surface(self, tmp_path):
        # Structural framelessness: a traced mark generates a boolean
        # polygon carve — no surface() heightmap anywhere in the SCAD.
        from kiln.emboss_generator import generate_emboss_scad
        from kiln.image_to_surface import prepare_logo_image_for_emboss

        p = _write_png(
            tmp_path, "logo.png", lambda d: d.rectangle([30, 20, 79, 59], fill=0)
        )
        info = prepare_logo_image_for_emboss(p, str(tmp_path))
        stl = os.path.join(str(tmp_path), "cube.stl")
        _write_cube_stl(stl, 40.0)
        face = {
            "face_name": "top",
            "center": (0.0, 0.0, 40.0),
            "normal": [0.0, 0.0, 1.0],
            "width_mm": 40.0,
            "height_mm": 40.0,
        }
        result = generate_emboss_scad(
            model_path=stl,
            content_info=info,
            face=face,
            output_dir=str(tmp_path),
            depth_mm=1.2,
            mode="deboss",
            scale=0.7,
        )
        scad = open(result["scad_path"]).read()
        assert "polygon(" in scad
        assert "surface(" not in scad
        assert "difference" in scad

    def test_tangent_glyph_rings_deboss_is_watertight(self, tmp_path):
        """Replays the pinched-edge incident with the geometry that caused it.

        The rings below are the arm and stem of a real wordmark glyph, taken
        from the mesh that surfaced this: one even-odd group whose sub-strokes
        run tangent, the curve-flattened arm grazing the stem's straight edge.
        Extruded as-is they produce edges carrying four triangles — a pinch no
        repair pass can sew, because it is not a hole.  The grow-by-epsilon
        wrap in to_scad() has to dissolve the tangency at the source.

        Straight-edged stand-ins do NOT reproduce this (tangent squares union
        cleanly); the flattened curve against a straight edge is the case that
        does, which is why the fixture is real geometry rather than a sketch.
        """
        import subprocess

        if not _openscad_available():
            pytest.skip("needs OpenSCAD")

        from kiln.generation.validation import count_non_manifold_edges

        mark = MarkGeometry(
            groups=[[_GLYPH_ARM, _GLYPH_STEM]], width=98.896, height=116.4
        )
        scad_p = os.path.join(str(tmp_path), "glyph.scad")
        with open(scad_p, "w") as f:
            f.write(
                "difference() {\n"
                "  translate([-120,-120,0]) cube([240,240,10]);\n"
                f"  translate([0,0,9]) linear_extrude(height=2) {mark.to_scad()}\n"
                "}\n"
            )
        out_stl = os.path.join(str(tmp_path), "decorated.stl")
        subprocess.run(
            ["openscad", "-q", "-o", out_stl, "--export-format", "binstl", scad_p],
            capture_output=True,
            check=True,
        )
        census = count_non_manifold_edges(out_stl)
        assert census["t_junction_edges"] == 0, census
        assert census["is_watertight"], census

    def test_fill_wrapper_skipped_for_holed_marks(self, monkeypatch):
        from kiln import emboss_generator

        monkeypatch.setattr(
            emboss_generator, "get_openscad_version", lambda *a, **k: "2025.01"
        )
        holed = {
            "openscad_polygons": "union() { polygon(points=[[0,0],[1,0],[1,1]], paths=[[0,1,2]], convexity=10); }",
            "openscad_polygons_fill_safe": False,
            "width": 10,
            "height": 10,
        }
        block = emboss_generator._svg_content_block(holed, 1.0, 1.0, 0.0, 0.0)
        assert "fill()" not in block
        legacy = {
            "openscad_polygons": "union() { hull() {} }",
            "width": 10,
            "height": 10,
        }
        block2 = emboss_generator._svg_content_block(legacy, 1.0, 1.0, 0.0, 0.0)
        assert "fill()" in block2  # legacy hull-fragment path keeps the glue


# Real tangent sub-strokes of a wordmark glyph — the geometry that
# produced the pinched edges this module's regression test replays.
_GLYPH_ARM = [
    (-32.1040, -33.9120), (-32.1040, -4.1200), (-31.6079, -3.5162),
    (-31.1115, -2.9130), (-30.6149, -2.3102), (-30.1180, -1.7080),
    (-29.6209, -1.1063), (-29.1235, -0.5050), (-28.6259, 0.0957),
    (-28.1280, 0.6960), (-27.6299, 1.2958), (-27.1315, 1.8950), (-26.6329, 2.4937),
    (-26.1340, 3.0920), (-25.6349, 3.6898), (-25.1355, 4.2870), (-24.6359, 4.8837),
    (-24.1360, 5.4800), (-23.6360, 6.0760), (-23.1360, 6.6720), (-22.6360, 7.2680),
    (-22.1360, 7.8640), (-21.6360, 8.4600), (-21.1360, 9.0560), (-20.6360, 9.6520),
    (-20.1360, 10.2480), (-19.6360, 10.8440), (-19.1360, 11.4400),
    (-18.6360, 12.0360), (-18.1360, 12.6320), (-17.6360, 13.2280),
    (-17.1360, 13.8240), (-16.6360, 14.4200), (-16.1360, 15.0160),
    (-15.6359, 15.6140), (-15.1357, 16.2120), (-14.6354, 16.8100),
    (-14.1350, 17.4080), (-13.6344, 18.0060), (-13.1337, 18.6040),
    (-12.6329, 19.2020), (-12.1320, 19.8000), (-11.6309, 20.3980),
    (-11.1297, 20.9960), (-10.6284, 21.5940), (-10.1270, 22.1920),
    (-9.6254, 22.7900), (-9.1237, 23.3880), (-8.6219, 23.9860), (-8.1200, 24.5840),
    (20.6000, 58.2000), (49.4480, 58.2000), (0.3120, 1.4800), (-1.6080, 1.4800)
]

_GLYPH_STEM = [
    (-49.4480, -58.2000), (-49.4480, 58.2000), (-25.4160, 58.2000),
    (-25.4160, 26.0560), (-25.4160, -3.4000), (-25.4160, -14.8720),
    (-25.4160, -58.2000)
]

def _openscad_available() -> bool:
    import subprocess

    try:
        subprocess.run(["openscad", "--version"], capture_output=True, check=True)
        return True
    except Exception:  # noqa: BLE001 — any failure means "not available"
        return False


def _write_cube_stl(path: str, size: float) -> None:
    """Minimal ASCII cube STL (12 triangles) for generator smoke tests."""
    s = size
    v = [
        (0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
        (0, 0, s), (s, 0, s), (s, s, s), (0, s, s),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2),  # bottom
        (4, 5, 6), (4, 6, 7),  # top
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "w") as f:
        f.write("solid cube\n")
        for a, b, c in faces:
            f.write("facet normal 0 0 0\nouter loop\n")
            for idx in (a, b, c):
                f.write(f"vertex {v[idx][0]} {v[idx][1]} {v[idx][2]}\n")
            f.write("endloop\nendfacet\n")
        f.write("endsolid cube\n")


class TestWhiteInkTransparency:
    """The light variant of a brand mark: white ink, transparent surround.

    Flattened onto white it is invisible, so before the alpha-as-ink
    fallback the trace door raised ValueError and the heightmap fallback
    produced a blank part.  The mark's geometry lives in the alpha
    channel, and ink colour never changes carve geometry — the white
    variant must trace exactly like the dark variant.
    """

    def _logo(self, tmp_path, fill):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([120, 90, 280, 120], fill=fill)
        d.rectangle([170, 120, 230, 220], fill=fill)
        p = tmp_path / f"logo_{fill[0]}.png"
        img.save(p)
        return str(p)

    def test_white_ink_is_bilevel(self, tmp_path):
        from kiln.mark_geometry import is_bilevel_image

        assert is_bilevel_image(self._logo(tmp_path, (255, 255, 255, 255)))

    def test_white_ink_traces_like_dark_ink(self, tmp_path):
        from kiln.mark_geometry import trace_image_to_mark

        white = trace_image_to_mark(self._logo(tmp_path, (255, 255, 255, 255)))
        dark = trace_image_to_mark(self._logo(tmp_path, (0, 0, 0, 255)))
        assert white is not None and not white.is_empty
        assert dark is not None and not dark.is_empty
        # Same alpha coverage -> same traced geometry, whatever the colour.
        assert white.width == pytest.approx(dark.width, rel=0.02)
        assert white.height == pytest.approx(dark.height, rel=0.02)
        assert sum(len(g) for g in white.groups) == sum(
            len(g) for g in dark.groups
        )
