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

import math
import os

import pytest

from kiln.mark_geometry import (
    MarkGeometry,
    _ring_area,
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
        assert m.width == pytest.approx(84.0, abs=0.5)  # 80 + width caps
        assert m.height == pytest.approx(4.0, abs=0.5)

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
        assert info["openscad_polygons"].startswith("union()")
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
