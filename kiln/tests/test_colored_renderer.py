"""Tests for PIL-based colored mesh renderer."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

import kiln.colored_renderer as colored_renderer
from kiln._vec import dot as _dot
from kiln.colored_renderer import (
    _CREASE_ANGLE_DEG,
    _apply_brightness,
    _compute_brightness,
    _darken,
    _face_normal,
    _smooth_face_normals,
    render_colored_mesh,
    render_colored_mesh_multi_angle,
)
from kiln.threemf_parser import ColoredTriangle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_colored_box() -> list[ColoredTriangle]:
    """A minimal box with 12 triangles, 6 colors (one per face)."""
    # Unit cube: 8 vertices, 12 triangles, 6 faces
    v = [
        (0.0, 0.0, 0.0),  # 0
        (10.0, 0.0, 0.0),  # 1
        (10.0, 10.0, 0.0),  # 2
        (0.0, 10.0, 0.0),  # 3
        (0.0, 0.0, 10.0),  # 4
        (10.0, 0.0, 10.0),  # 5
        (10.0, 10.0, 10.0),  # 6
        (0.0, 10.0, 10.0),  # 7
    ]
    # 6 face colors (red, green, blue, yellow, cyan, magenta)
    face_colors = [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 0),
        (0, 255, 255),
        (255, 0, 255),
    ]
    # Each face = 2 triangles with same color
    faces = [
        # Bottom (Z=0)
        ((0, 1, 2), (0, 2, 3)),
        # Top (Z=10)
        ((4, 6, 5), (4, 7, 6)),
        # Front (Y=0)
        ((0, 5, 1), (0, 4, 5)),
        # Back (Y=10)
        ((2, 7, 3), (2, 6, 7)),
        # Left (X=0)
        ((0, 7, 4), (0, 3, 7)),
        # Right (X=10)
        ((1, 6, 2), (1, 5, 6)),
    ]
    triangles: list[ColoredTriangle] = []
    for fi, (t1_idx, t2_idx) in enumerate(faces):
        color = face_colors[fi]
        triangles.append(ColoredTriangle(
            v0=v[t1_idx[0]], v1=v[t1_idx[1]], v2=v[t1_idx[2]], color=color,
        ))
        triangles.append(ColoredTriangle(
            v0=v[t2_idx[0]], v1=v[t2_idx[1]], v2=v[t2_idx[2]], color=color,
        ))
    return triangles


def _make_single_triangle() -> list[ColoredTriangle]:
    return [
        ColoredTriangle(
            v0=(0.0, 0.0, 0.0),
            v1=(10.0, 0.0, 0.0),
            v2=(5.0, 10.0, 0.0),
            color=(255, 128, 0),
        ),
    ]


def _make_capped_cylinder(
    *,
    sections: int = 64,
    radius: float = 20.0,
    height: float = 50.0,
    color: tuple[int, int, int] = (200, 60, 60),
    top_color: tuple[int, int, int] | None = None,
) -> list[ColoredTriangle]:
    """Closed cylinder: wall quads split into tall triangle pairs + cap fans.

    The shape that exposed the flat-shading striping defect: adjacent
    tall wall triangles differ slightly in orientation, so per-facet
    lighting renders a smooth wall as vertical stripes.  With
    *top_color* set, the wall splits into two color bands at half
    height (band boundary vertices shared bit-exactly across bands).
    """
    tris: list[ColoredTriangle] = []
    band_edges = [0.0, height] if top_color is None else [0.0, height / 2.0, height]
    band_colors = [color] if top_color is None else [color, top_color]
    for k in range(sections):
        a0 = 2 * math.pi * k / sections
        a1 = 2 * math.pi * (k + 1) / sections
        x0, y0 = radius * math.cos(a0), radius * math.sin(a0)
        x1, y1 = radius * math.cos(a1), radius * math.sin(a1)
        for band, band_color in enumerate(band_colors):
            zb, zt = band_edges[band], band_edges[band + 1]
            p00, p10 = (x0, y0, zb), (x1, y1, zb)
            p01, p11 = (x0, y0, zt), (x1, y1, zt)
            tris.append(ColoredTriangle(v0=p00, v1=p10, v2=p11, color=band_color))
            tris.append(ColoredTriangle(v0=p00, v1=p11, v2=p01, color=band_color))
        # Cap fans (bottom gets the first color, top the last)
        zt = band_edges[-1]
        tris.append(ColoredTriangle(
            v0=(0.0, 0.0, 0.0), v1=(x1, y1, 0.0), v2=(x0, y0, 0.0),
            color=band_colors[0],
        ))
        tris.append(ColoredTriangle(
            v0=(0.0, 0.0, zt), v1=(x0, y0, zt), v2=(x1, y1, zt),
            color=band_colors[-1],
        ))
    return tris


def _wall_indices(tris: list[ColoredTriangle]) -> list[int]:
    """Indices of wall faces (non-horizontal: some vertex Z differs)."""
    return [
        i for i, t in enumerate(tris)
        if not (t.v0[2] == t.v1[2] == t.v2[2])
    ]


def _adjacent_pairs(
    tris: list[ColoredTriangle], indices: list[int],
) -> list[tuple[int, int]]:
    """Pairs of faces (within *indices*) sharing an exact edge."""
    edge_map: dict[tuple, list[int]] = {}
    for i in indices:
        verts = (tris[i].v0, tris[i].v1, tris[i].v2)
        for j in range(3):
            edge = tuple(sorted((verts[j], verts[(j + 1) % 3])))
            edge_map.setdefault(edge, []).append(i)
    return [(f[0], f[1]) for f in edge_map.values() if len(f) == 2]


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


class TestMathHelpers:
    """Pure-math vector helper functions."""

    def test_face_normal_unit_length(self) -> None:
        n = _face_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
        assert abs(length - 1.0) < 1e-6

    def test_face_normal_z_up(self) -> None:
        n = _face_normal((0, 0, 0), (1, 0, 0), (0, 1, 0))
        assert abs(n[2] - 1.0) < 1e-6 or abs(n[2] + 1.0) < 1e-6

    def test_compute_brightness_range(self) -> None:
        # Should always be between ambient and 1.0
        for nx in (-1, 0, 1):
            for ny in (-1, 0, 1):
                for nz in (-1, 0, 1):
                    b = _compute_brightness((nx, ny, nz), (0.3, -0.6, 0.7))
                    assert 0.0 <= b <= 1.0

    def test_apply_brightness_clamps(self) -> None:
        # Full brightness
        assert _apply_brightness((255, 255, 255), 1.0) == (255, 255, 255)
        # Zero brightness — shadow floor preserves color, never full black
        r, g, b = _apply_brightness((255, 255, 255), 0.0)
        assert r == g == b
        assert r > 0  # shadow floor prevents crush to black
        assert r < 128  # but still visibly dark

    def test_darken(self) -> None:
        assert _darken((200, 100, 50), factor=0.5) == (100, 50, 25)


# ---------------------------------------------------------------------------
# Smooth shading normals
# ---------------------------------------------------------------------------


class TestSmoothFaceNormals:
    """Crease-aware vertex-normal smoothing for lighting."""

    def test_crease_threshold_in_convention_band(self) -> None:
        # 30-40 degrees is the CAD/DCC auto-smoothing convention; a 12+
        # section cylinder (facets <= 30 deg) must smooth, a cube edge
        # (90 deg) must stay hard.
        assert 30.0 <= _CREASE_ANGLE_DEG <= 40.0

    def test_cylinder_wall_normals_near_true_surface_normal(self) -> None:
        # Each wall face's lighting normal should approximate the TRUE
        # cylinder surface normal (radial) at its centroid — that is what
        # eliminates the striping.  Measured worst deviation at 64
        # sections: 1.56 deg; bound at 2.5 deg.
        tris = _make_capped_cylinder()
        smoothed = _smooth_face_normals([(t.v0, t.v1, t.v2) for t in tris])
        min_dot = math.cos(math.radians(2.5))
        for i in _wall_indices(tris):
            t = tris[i]
            cx = (t.v0[0] + t.v1[0] + t.v2[0]) / 3.0
            cy = (t.v0[1] + t.v1[1] + t.v2[1]) / 3.0
            ln = math.hypot(cx, cy)
            radial = (cx / ln, cy / ln, 0.0)
            assert _dot(smoothed[i], radial) > min_dot

    def test_cylinder_adjacent_wall_triangles_near_identical(self) -> None:
        # Adjacent tall wall triangles must get near-identical lighting
        # normals.  Flat facet normals differ by the full facet angle
        # (5.625 deg at 64 sections) with parity artifacts; smoothed
        # normals stay within 4 deg (measured worst: 3.12 deg).
        tris = _make_capped_cylinder()
        verts = [(t.v0, t.v1, t.v2) for t in tris]
        smoothed = _smooth_face_normals(verts)
        flats = [_face_normal(*v) for v in verts]
        pairs = _adjacent_pairs(tris, _wall_indices(tris))
        assert pairs
        smooth_bound = math.cos(math.radians(4.0))
        for a, b in pairs:
            assert _dot(smoothed[a], smoothed[b]) > smooth_bound
        # The flat normals DO exceed that spread — proves smoothing is
        # what closes the gap, not the geometry being trivially smooth.
        worst_flat = min(_dot(flats[a], flats[b]) for a, b in pairs)
        assert worst_flat < math.cos(math.radians(5.0))

    def test_cylinder_caps_stay_flat(self) -> None:
        # Cap fans are coplanar and meet the wall at 90 deg — above the
        # crease threshold, so their lighting normals stay exactly flat.
        tris = _make_capped_cylinder()
        verts = [(t.v0, t.v1, t.v2) for t in tris]
        smoothed = _smooth_face_normals(verts)
        wall = set(_wall_indices(tris))
        for i, v in enumerate(verts):
            if i in wall:
                continue
            assert _dot(smoothed[i], _face_normal(*v)) > 1.0 - 1e-9

    def test_cube_faces_stay_flat_shaded(self) -> None:
        # Smoothing is purely geometric (colors never enter), so the ONLY
        # thing keeping a cube's edges hard is the crease threshold (90
        # deg dihedrals): every face's smoothed normal must equal its
        # flat normal exactly — a cube still shades as six flat faces.
        box = _make_colored_box()
        verts = [(t.v0, t.v1, t.v2) for t in box]
        smoothed = _smooth_face_normals(verts)
        for v, s in zip(verts, smoothed, strict=True):
            assert _dot(s, _face_normal(*v)) > 1.0 - 1e-9

    def test_hinge_below_crease_smooths_above_stays_hard(self) -> None:
        def hinge(dihedral_deg: float) -> list[tuple]:
            # Two triangles sharing the edge (0,0,0)-(10,0,0); the second
            # folded up by *dihedral_deg* out of the XY plane.
            a = math.radians(dihedral_deg)
            return [
                ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.0, 10.0, 0.0)),
                (
                    (0.0, 0.0, 0.0),
                    (5.0, -10.0 * math.cos(a), 10.0 * math.sin(a)),
                    (10.0, 0.0, 0.0),
                ),
            ]

        # Below threshold: normals pull toward each other.
        verts = hinge(20.0)
        smoothed = _smooth_face_normals(verts)
        flats = [_face_normal(*v) for v in verts]
        assert _dot(smoothed[0], smoothed[1]) > _dot(flats[0], flats[1])
        assert _dot(smoothed[0], flats[0]) < 1.0 - 1e-6  # actually moved
        # Above threshold: both faces keep their exact flat normals.
        verts = hinge(60.0)
        smoothed = _smooth_face_normals(verts)
        flats = [_face_normal(*v) for v in verts]
        for s, f in zip(smoothed, flats, strict=True):
            assert _dot(s, f) > 1.0 - 1e-9

    def test_degenerate_triangle_does_not_crash(self) -> None:
        verts = [
            ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.0, 10.0, 0.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),  # degenerate
        ]
        smoothed = _smooth_face_normals(verts)
        assert len(smoothed) == 2
        assert smoothed[1] == (0.0, 0.0, 0.0)  # flat-normal fallback


class TestSmoothShadingRendered:
    """The smoothing must survive to the rendered pixels."""

    def _midrow_max_step(self, path: str) -> float:
        """Largest luminance jump between adjacent wall pixels, mid-row."""
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
            row = h // 2
            lums = []
            for x in range(w):
                r, g, b = img.getpixel((x, row))[:3]
                if r > g + 20 and r > b + 20:  # red-family wall pixel
                    lums.append((r * 299 + g * 587 + b * 114) / 1000)
        lums = lums[3:-3]  # skip silhouette-contour pixels at the edges
        return max(abs(lums[i + 1] - lums[i]) for i in range(len(lums) - 1))

    def test_cylinder_wall_renders_without_striping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Rendered wall brightness must vary smoothly across columns.
        # Measured max step: 1.6 with smoothing, 2.9 with flat facet
        # normals — the flat run proves this metric catches the defect.
        tris = _make_capped_cylinder()
        kwargs = dict(
            width=240, height=240, elevation=15, azimuth=30, supersample=1,
        )
        smooth_png = str(tmp_path / "smooth.png")
        render_colored_mesh(tris, output_path=smooth_png, **kwargs)
        assert self._midrow_max_step(smooth_png) <= 2.2

        # Regression twin: force flat facet lighting and the striping
        # comes back — the metric CAN fail.
        monkeypatch.setattr(
            colored_renderer,
            "_smooth_face_normals",
            lambda verts: [_face_normal(*v) for v in verts],
        )
        flat_png = str(tmp_path / "flat.png")
        render_colored_mesh(tris, output_path=flat_png, **kwargs)
        assert self._midrow_max_step(flat_png) >= 2.5

    def test_two_color_boundary_keeps_exact_colors(self, tmp_path: Path) -> None:
        # Lighting smooths across the paint boundary, but the COLORS must
        # not: with pure red and pure blue bands, every rendered pixel is
        # gray (background/outline), pure-red family, or pure-blue family.
        # Any channel mixing would prove color bleed across the boundary.
        tris = _make_capped_cylinder(
            color=(255, 0, 0), top_color=(0, 0, 255),
        )
        out = str(tmp_path / "bands.png")
        render_colored_mesh(
            tris, output_path=out,
            width=240, height=240, elevation=15, azimuth=30, supersample=1,
        )
        from PIL import Image

        reds = blues = 0
        with Image.open(out) as img:
            for x in range(img.width):
                for y in range(img.height):
                    r, g, b = img.getpixel((x, y))[:3]
                    if r == g == b:
                        continue  # background / outline gray
                    if g == 0 and b == 0:
                        reds += 1
                    elif r == 0 and g == 0:
                        blues += 1
                    else:
                        raise AssertionError(
                            f"mixed color at ({x},{y}): {(r, g, b)}"
                        )
        assert reds > 100
        assert blues > 100


# ---------------------------------------------------------------------------
# render_colored_mesh
# ---------------------------------------------------------------------------


class TestRenderColoredMesh:
    """Single-angle colored mesh rendering."""

    def test_produces_valid_png(self, tmp_path: Path) -> None:
        out = str(tmp_path / "test.png")
        result = render_colored_mesh(
            _make_colored_box(),
            output_path=out,
            width=400,
            height=300,
            supersample=1,
        )
        assert os.path.isfile(out)
        assert os.path.getsize(out) > 0
        assert result.path == out
        assert result.width == 400
        assert result.height == 300
        assert result.triangle_count == 12
        # Back-face culling hides some faces, so not all 6 colors are visible
        assert result.face_colors_used >= 3

    def test_supersample_downscales(self, tmp_path: Path) -> None:
        out = str(tmp_path / "ss.png")
        result = render_colored_mesh(
            _make_single_triangle(),
            output_path=out,
            width=200,
            height=150,
            supersample=2,
        )
        from PIL import Image

        with Image.open(out) as img:
            assert img.size == (200, 150)
        assert result.face_colors_used == 1

    def test_default_output_path(self) -> None:
        result = render_colored_mesh(
            _make_single_triangle(),
            width=100,
            height=100,
            supersample=1,
        )
        assert os.path.isfile(result.path)
        assert result.path.endswith(".png")
        # Clean up
        os.unlink(result.path)

    def test_empty_triangles_raises(self) -> None:
        with pytest.raises(ValueError, match="No triangles"):
            render_colored_mesh([])

    def test_to_dict(self, tmp_path: Path) -> None:
        out = str(tmp_path / "dict.png")
        result = render_colored_mesh(
            _make_single_triangle(),
            output_path=out,
            supersample=1,
        )
        d = result.to_dict()
        assert d["path"] == out
        assert isinstance(d["width"], int)
        assert isinstance(d["face_colors_used"], int)


# ---------------------------------------------------------------------------
# render_colored_mesh_multi_angle
# ---------------------------------------------------------------------------


class TestRenderMultiAngle:
    """Multi-angle colored mesh rendering."""

    def test_all_six_angles(self, tmp_path: Path) -> None:
        views = render_colored_mesh_multi_angle(
            _make_colored_box(),
            output_dir=str(tmp_path),
            width=200,
            height=150,
            supersample=1,
        )
        assert len(views) == 6
        angles = [v["angle"] for v in views]
        assert "isometric" in angles
        assert "front" in angles
        assert "right" in angles
        assert "top" in angles
        assert "bottom" in angles
        assert "back" in angles

        for v in views:
            assert os.path.isfile(v["path"])
            assert v["description"]

    def test_subset_of_angles(self, tmp_path: Path) -> None:
        views = render_colored_mesh_multi_angle(
            _make_colored_box(),
            output_dir=str(tmp_path),
            angles=["isometric", "top"],
            width=200,
            height=150,
            supersample=1,
        )
        assert len(views) == 2
        # Canonical order preserved (isometric first)
        assert views[0]["angle"] == "isometric"
        assert views[1]["angle"] == "top"
        # Quality scores present as metadata
        assert "quality_score" in views[0]

    def test_unknown_angle_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown camera angles"):
            render_colored_mesh_multi_angle(
                _make_single_triangle(),
                angles=["diagonal"],
            )

    def test_empty_triangles_raises(self) -> None:
        with pytest.raises(ValueError, match="No triangles"):
            render_colored_mesh_multi_angle([])


class TestDepthBufferOcclusion:
    """Visibility is decided per PIXEL — the case a whole-face sort cannot
    get right.  Faces sorted by centroid depth and painted back-to-front
    mis-paint any screen overlap whose depth order crosses; on composed
    multi-part plates that measured 3.9-10.8% wrong-colour pixels."""

    @staticmethod
    def _crossing_walls() -> list[ColoredTriangle]:
        red = (255, 0, 0)
        blue = (0, 0, 255)
        # Wall A slopes near-left to far-right (y = x/2); wall B mirrors it
        # (y = -x/2).  Viewed along +Y they project onto the SAME screen
        # rectangle, with A nearer on the left half and B nearer on the
        # right — no single per-face depth key orders that correctly.
        a = [
            (-10.0, -5.0, 0.0), (10.0, 5.0, 0.0),
            (10.0, 5.0, 10.0), (-10.0, -5.0, 10.0),
        ]
        b = [
            (-10.0, 5.0, 0.0), (10.0, -5.0, 0.0),
            (10.0, -5.0, 10.0), (-10.0, 5.0, 10.0),
        ]
        tris: list[ColoredTriangle] = []
        for quad, color in ((a, red), (b, blue)):
            bl, br, tr, tl = quad
            tris.append(ColoredTriangle(v0=bl, v1=br, v2=tr, color=color))
            tris.append(ColoredTriangle(v0=bl, v1=tr, v2=tl, color=color))
        return tris

    def test_crossing_walls_show_the_nearer_color_on_both_sides(
        self, tmp_path: Path
    ) -> None:
        from PIL import Image

        width, height = 200, 150
        out = tmp_path / "crossing.png"
        render_colored_mesh(
            self._crossing_walls(),
            output_path=str(out),
            width=width,
            height=height,
            elevation=0.0,
            azimuth=0.0,
            supersample=1,
        )
        img = Image.open(out).convert("RGB")

        # Probe via the renderer's own fit: model x spans -10..10 (the
        # wider axis), so sf = width * margin / 20; screen x = center +
        # model_x * sf, screen y = center - (z - 5) * sf.
        sf = width * 0.85 / 20.0

        def probe(model_x: float, model_z: float) -> tuple[int, int, int]:
            px = int(width / 2.0 + model_x * sf)
            py = int(height / 2.0 - (model_z - 5.0) * sf)
            return img.getpixel((px, py))

        # Both probes sit in the screen regions the old whole-face sort
        # painted with the FARTHER wall's color (draw order put a blue
        # triangle last over the lower half and a red one over the upper).
        left = probe(-5.0, 2.0)   # wall A (red) is nearer at x=-5
        right = probe(5.0, 8.0)   # wall B (blue) is nearer at x=+5
        assert left[0] > left[2], f"left probe should be red-dominant, got {left}"
        assert right[2] > right[0], f"right probe should be blue-dominant, got {right}"


class TestSilhouetteContourStaysOnTheOutline:
    """The contour marks the object against the BACKGROUND, nothing else.

    It used to mark "an edge carried by exactly one VISIBLE face", which
    back-face culling also reports for interior edges — the top rim of a
    near wall, the seam where a boolean re-triangulates a surface — so a
    hollow or unioned model got hairlines drawn across its body.  It hid
    in single-colour previews because bright fills skip the contour, so a
    grey control render looked clean while every painted one did not.
    """

    @staticmethod
    def _open_box() -> list[ColoredTriangle]:
        """A tray: solid walls with a cavity, so the near rim is interior.

        Looking in from above, the near wall's top edge has a visible
        outer face and a culled inner one, and it sits WELL INSIDE the
        object's outline — pixels beyond it are the tray's interior, not
        background.  That edge is the reproducer.
        """
        trimesh = pytest.importorskip("trimesh", reason="tray fixture needs trimesh")
        outer = trimesh.creation.box(extents=[40, 40, 24])
        cavity = trimesh.creation.box(extents=[32, 32, 20])
        cavity.apply_translation([0, 0, 4])
        tray = outer.difference(cavity)
        # Dark enough that the contour is not skipped, and saturated so an
        # object pixel is told apart from a neutral contour pixel by hue.
        return [
            ColoredTriangle(
                v0=tuple(map(float, t[0])),
                v1=tuple(map(float, t[1])),
                v2=tuple(map(float, t[2])),
                color=(70, 22, 22),
            )
            for t in tray.triangles
        ]

    def test_no_contour_is_drawn_inside_the_object(self, tmp_path: Path) -> None:
        from PIL import Image

        out = str(tmp_path / "tray.png")
        render_colored_mesh(
            self._open_box(),
            output_path=out,
            width=420,
            height=340,
            elevation=38.0,
            azimuth=32.0,
            supersample=2,
        )
        img = Image.open(out).convert("RGB")
        w, h = img.size
        px = img.load()

        # The fill is red-dominant; the contour is a neutral grey lifted off
        # the background.  So "object" is hue, not brightness.
        def saturated(x: int, y: int) -> bool:
            r, g, b = px[x, y]
            return r - g > 12 and r - b > 12

        rows = [[saturated(x, y) for x in range(w)] for y in range(h)]
        assert sum(map(sum, rows)) > 4000, "fixture sanity: the tray fills the frame"

        # A stray is a NON-object pixel with object on all four sides — it
        # can only be something drawn over the body.  The legitimate
        # outline ring never qualifies: it has background on one side.
        # (Judging "object pixels that look wrong" instead would be
        # circular, since a contour pixel is not object-coloured and so
        # would never enter the set being judged.)
        strays = []
        for y in range(h):
            row = rows[y]
            for x in range(w):
                if row[x]:
                    continue
                if not (any(row[:x]) and any(row[x + 1 :])):
                    continue
                above = any(rows[k][x] for k in range(y))
                below = any(rows[k][x] for k in range(y + 1, h))
                if above and below:
                    strays.append((x, y))

        assert not strays, (
            f"{len(strays)} contour pixel(s) drawn inside the object, "
            f"e.g. {strays[:5]} — the contour must only mark the outline"
        )


# ---------------------------------------------------------------------------
# Diagram shading — the render modes an image whose colours are LABELS uses
# ---------------------------------------------------------------------------


def _make_flat_ring(
    *,
    sections: int = 96,
    inner: float = 10.0,
    outer: float = 20.0,
    color: tuple[int, int, int] = (120, 140, 170),
) -> list[ColoredTriangle]:
    """A flat annulus in the Z=0 plane, normals +Z.

    Its area-weighted centroid is the middle of the HOLE, which is what
    makes it the shape that catches a callout anchor placed by averaging.
    """
    tris: list[ColoredTriangle] = []
    for k in range(sections):
        a0 = 2 * math.pi * k / sections
        a1 = 2 * math.pi * (k + 1) / sections
        i0 = (inner * math.cos(a0), inner * math.sin(a0), 0.0)
        i1 = (inner * math.cos(a1), inner * math.sin(a1), 0.0)
        o0 = (outer * math.cos(a0), outer * math.sin(a0), 0.0)
        o1 = (outer * math.cos(a1), outer * math.sin(a1), 0.0)
        tris.append(ColoredTriangle(v0=i0, v1=o0, v2=o1, color=color))
        tris.append(ColoredTriangle(v0=i0, v1=o1, v2=i1, color=color))
    return tris


class TestMatteShading:
    """``shading="matte"``: lambert only, no rim, no highlight."""

    def test_matte_shade_depends_only_on_the_light_angle(self) -> None:
        # Two normals with the SAME angle to the light but different
        # angles to the CAMERA.  The rim term keys off the camera angle,
        # so studio separates them; matte, having no rim, must not.
        light = colored_renderer._normalize((0.0, -0.6, 0.8))
        # Equal angle to the light; the first is edge-on to the camera
        # (where the rim fires), the second turned toward it.
        at_silhouette = colored_renderer._normalize((0.9682, 0.0, 0.25))
        toward_camera = colored_renderer._normalize((0.857, -0.5, -0.125))
        assert abs(_dot(at_silhouette, light) - _dot(toward_camera, light)) < 1e-3
        assert abs(abs(at_silhouette[1]) - abs(toward_camera[1])) > 0.4

        studio_rim = _compute_brightness(at_silhouette, light, face_luminance=40.0)
        studio_plain = _compute_brightness(toward_camera, light, face_luminance=40.0)
        assert studio_rim - studio_plain > 0.05, (
            "fixture sanity: the studio path must actually apply a rim here"
        )

        matte_rim = colored_renderer._matte_shade(at_silhouette, light)
        matte_plain = colored_renderer._matte_shade(toward_camera, light)
        # Only the 1e-3 slack in the two dot products, versus the studio
        # path's 0.2 of pure camera-angle highlight.
        assert matte_rim == pytest.approx(matte_plain, abs=1e-3)

    def test_matte_stays_close_to_the_declared_color(self) -> None:
        # A legend swatch is drawn in the raw colour; the pixels it names
        # are drawn shaded.  If shading can drive a fill far from its own
        # colour, the legend stops describing the picture.
        assert colored_renderer._matte_shade((0.0, 0.0, 1.0), (0.0, 0.0, -1.0)) >= 0.7

    def test_rendered_matte_has_no_silhouette_lift(self, tmp_path: Path) -> None:
        # A dark cylinder is where the adaptive rim is strongest.  Scan a
        # mid-row across the wall: under studio lighting the columns next
        # to the silhouette are LIFTED above the row's darkest pixel;
        # under matte they are the darkest thing in the row.
        # Both renders sit on the same flat pale field, so shading is the
        # only thing that can differ.  A dark object on a light field also
        # keeps "object" and "background" cleanly separable by luminance,
        # which a lifted edge pixel must not be able to blur.
        tris = _make_capped_cylinder(color=(55, 58, 70))
        kwargs = dict(
            width=240, height=240, elevation=10, azimuth=30, supersample=1,
            background=(250, 250, 250), background_gradient=False,
        )

        def _edge_lift(path: str) -> float:
            from PIL import Image

            with Image.open(path) as img:
                row = img.height // 2
                lums = [
                    (px[0] * 299 + px[1] * 587 + px[2] * 114) / 1000
                    for x in range(img.width)
                    for px in [img.getpixel((x, row))[:3]]
                ]
            # Object pixels only; trim the antialiased silhouette itself.
            wall = [v for v in lums if v < 180][3:-3]
            return min(wall[0], wall[-1]) - min(wall)

        studio = str(tmp_path / "studio.png")
        render_colored_mesh(tris, output_path=studio, **kwargs)
        matte = str(tmp_path / "matte.png")
        render_colored_mesh(tris, output_path=matte, shading="matte", **kwargs)

        # The regression twin: the metric CAN separate the two paths.
        assert _edge_lift(studio) > 2.0
        assert _edge_lift(matte) <= 0.5

    def test_unknown_shading_refuses(self) -> None:
        with pytest.raises(ValueError, match="unknown shading"):
            render_colored_mesh(_make_colored_box(), shading="glossy")


class TestBackgroundGradient:
    """``background_gradient=False``: no implied ground plane."""

    def _corners(self, path: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
        from PIL import Image

        with Image.open(path) as img:
            return (
                img.getpixel((2, 2))[:3],
                img.getpixel((2, img.height - 3))[:3],
            )

    def test_off_is_flat_and_on_is_not(self, tmp_path: Path) -> None:
        tris = _make_colored_box()
        kwargs = dict(width=200, height=200, supersample=1, background=(60, 60, 60))

        on = str(tmp_path / "gradient.png")
        render_colored_mesh(tris, output_path=on, **kwargs)
        top_on, bottom_on = self._corners(on)
        # The twin: with the gradient the two ends of the field differ.
        assert top_on != bottom_on

        off = str(tmp_path / "flat.png")
        render_colored_mesh(tris, output_path=off, background_gradient=False, **kwargs)
        top_off, bottom_off = self._corners(off)
        assert top_off == bottom_off == (60, 60, 60)


class TestFaceLabels:
    """``face_labels``: ask the renderer where a group landed."""

    def test_reports_an_anchor_and_a_size_per_label(self, tmp_path: Path) -> None:
        tris = _make_capped_cylinder(color=(200, 60, 60), top_color=(60, 60, 200))
        labels = [0 if tri.color == (200, 60, 60) else 1 for tri in tris]
        out = str(tmp_path / "labelled.png")
        result = render_colored_mesh(
            tris, output_path=out, width=200, height=200, supersample=1,
            face_labels=labels,
        )
        assert result.label_anchors is not None
        assert result.label_pixels is not None
        assert set(result.label_anchors) == {0, 1}
        assert all(v > 100 for v in result.label_pixels.values())
        for x, y in result.label_anchors.values():
            assert 0 <= x < 200 and 0 <= y < 200

    def test_anchor_lands_on_the_label_not_in_its_hole(self, tmp_path: Path) -> None:
        # A ring: every pixel it owns is far from its own average.  The
        # anchor must be a pixel the label actually owns, so it lands on
        # the ring — an unsnapped mean would sit in the empty middle.
        tris = _make_flat_ring()
        out = str(tmp_path / "ring.png")
        result = render_colored_mesh(
            tris, output_path=out, width=240, height=240, supersample=1,
            elevation=90, azimuth=0, face_labels=[0] * len(tris),
        )
        assert result.label_anchors is not None
        ax, ay = result.label_anchors[0]

        # Measure the unsnapped mean from the image itself — that is the
        # value the anchor must NOT be.
        from PIL import Image

        with Image.open(out) as img:
            owned = [
                (x, y)
                for y in range(img.height)
                for x in range(img.width)
                if img.getpixel((x, y))[:3] != (30, 30, 30)
                and abs(img.getpixel((x, y))[0] - img.getpixel((x, y))[2]) > 10
            ]
        mx = sum(p[0] for p in owned) / len(owned)
        my = sum(p[1] for p in owned) / len(owned)
        # The twin: the mean really does fall in the hole.
        assert math.dist((mx, my), (120, 120)) < 12
        assert math.dist((ax, ay), (mx, my)) > 20

    def test_hidden_label_gets_no_anchor(self, tmp_path: Path) -> None:
        # A label made only of down-facing faces is culled away entirely.
        # It must report nothing rather than a made-up point — a callout
        # drawn for a region nobody can see points at another region.
        tris = _make_capped_cylinder()
        labels = [
            9 if (tri.v0[2] == 0.0 and tri.v1[2] == 0.0 and tri.v2[2] == 0.0) else 0
            for tri in tris
        ]
        assert labels.count(9) > 10, "fixture sanity: the bottom cap exists"
        result = render_colored_mesh(
            tris, output_path=str(tmp_path / "cyl.png"),
            width=200, height=200, elevation=60, azimuth=45, supersample=1,
            face_labels=labels,
        )
        assert result.label_anchors is not None
        assert 0 in result.label_anchors
        assert 9 not in result.label_anchors

    def test_length_mismatch_refuses(self) -> None:
        with pytest.raises(ValueError, match="face_labels has"):
            render_colored_mesh(_make_colored_box(), face_labels=[0, 1])
