"""simplify_mesh returns the triangle count asked for, keeps a closed part closed,
and replies with what it measured.

Covers :func:`kiln.generation.validation.simplify_mesh` on a dense rippled
sphere (binary and ASCII STL) at gentle, middle and aggressive ratios; the
refusals (a part with no triangles to spare, a 3MF carrying two colors or two
objects) and the file they leave untouched; an open part; a part already at
the count asked for; the ``simplify_mesh_model`` tool's reply;
:mod:`kiln.surface_deviation` against a brute-force measure; and the package
that does the collapsing.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh

#: The fixture sphere: 50 mm across, its surface rippled 0.15 mm up and down.
_RADIUS_MM = 25.0
_RIPPLE_MM = 0.15
_WAVES = 24
#: A dense sphere: 327,680 triangles; and one of 81,920.
_DENSE = 7
_MODERATE = 6
#: How close a count must come to the count asked for.
_COUNT_SHARE = 0.02
#: How far the bounding box may move, and the volume change, as the engine allows them.
_OUTLINE_MM = 0.05
_VOLUME_SHARE = 0.03
#: The largest surface movement a quarter of the dense sphere may show: a
#: third of the ripple height, so a result that smoothed the ripples away
#: (about 0.15 mm off) fails.
_QUARTER_DEVIATION_MM = 0.05

_KILN_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixture meshes
# ---------------------------------------------------------------------------


def _rippled_sphere(subdivisions: int) -> np.ndarray:
    """``(N, 3, 3)`` float32 corners of the closed rippled sphere."""
    sphere = trimesh.creation.icosphere(subdivisions=subdivisions, radius=1.0)
    unit = np.asarray(sphere.vertices, dtype=np.float64)
    theta = np.arccos(np.clip(unit[:, 2], -1.0, 1.0))
    phi = np.arctan2(unit[:, 1], unit[:, 0])
    radius = _RADIUS_MM + _RIPPLE_MM * np.sin(_WAVES * theta) * np.sin(_WAVES * phi)
    return (unit * radius[:, None]).astype(np.float32)[np.asarray(sphere.faces)]


def _write_binary_stl(path: Path, corners: np.ndarray) -> Path:
    records = np.zeros(len(corners), dtype=[("normal", "<f4", (3,)), ("corners", "<f4", (3, 3)), ("attr", "<u2")])
    records["corners"] = corners
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(corners)) + records.tobytes())
    return path


def _write_ascii_stl(path: Path, corners: np.ndarray) -> Path:
    facet = (
        "facet normal 0 0 0\n outer loop\n  vertex %r %r %r\n  vertex %r %r %r\n"
        "  vertex %r %r %r\n endloop\nendfacet\n"
    )
    rows = corners.reshape(-1, 9).astype(np.float64).tolist()
    path.write_text("solid rippled\n" + "".join(facet % tuple(row) for row in rows) + "endsolid rippled\n")
    return path


def _box(size: float = 20.0) -> np.ndarray:
    box = trimesh.creation.box(extents=(size, size, size))
    return np.asarray(box.vertices, dtype=np.float32)[np.asarray(box.faces)]


def _tetrahedron() -> np.ndarray:
    points = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], dtype=np.float32)
    return points[np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])]


def _write_3mf(path: Path, objects: list[np.ndarray], *, two_colors: bool = False) -> Path:
    """A 3MF placing each ``(N, 3, 3)`` array as an object; with *two_colors*
    the first object's second half is red."""
    resources = []
    if two_colors:
        resources.append(
            '<basematerials id="100"><base name="White" displaycolor="#FFFFFF"/>'
            '<base name="Red" displaycolor="#FF0000"/></basematerials>'
        )
    items = []
    for number, corners in enumerate(objects, start=1):
        points, faces = np.unique(corners.reshape(-1, 3), axis=0, return_inverse=True)
        faces = faces.reshape(-1, 3)
        vertices = "".join(f'<vertex x="{x!r}" y="{y!r}" z="{z!r}"/>' for x, y, z in points.astype(np.float64).tolist())
        triangles = "".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}"'
            + (' pid="100" p1="1"' if two_colors and number == 1 and i >= len(faces) // 2 else "")
            + "/>"
            for i, (a, b, c) in enumerate(faces.tolist())
        )
        colored = ' pid="100" pindex="0"' if two_colors and number == 1 else ""
        resources.append(
            f'<object id="{number}" type="model"{colored}><mesh><vertices>{vertices}</vertices>'
            f"<triangles>{triangles}</triangles></mesh></object>"
        )
        items.append(f'<item objectid="{number}"/>')
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{''.join(resources)}</resources><build>{''.join(items)}</build></model>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        zf.writestr("3D/3dmodel.model", model)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_back(path: str | Path) -> trimesh.Trimesh:
    return trimesh.load(str(path), force="mesh")


def _open_edges(mesh: trimesh.Trimesh) -> int:
    _edges, uses = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return int((uses == 1).sum())


def _simplify(path: Path, ratio: float, output: Path | None = None) -> dict:
    from kiln.generation.validation import simplify_mesh

    return simplify_mesh(str(path), target_ratio=ratio, output_path=None if output is None else str(output))


@pytest.fixture(scope="module")
def dense(tmp_path_factory) -> dict:
    folder = tmp_path_factory.mktemp("dense")
    corners = _rippled_sphere(_DENSE)
    return {
        "binary": _write_binary_stl(folder / "rippled.stl", corners),
        "ascii": _write_ascii_stl(folder / "rippled_ascii.stl", corners),
        "triangles": len(corners),
    }


@pytest.fixture(scope="module")
def moderate(tmp_path_factory) -> dict:
    corners = _rippled_sphere(_MODERATE)
    return {"binary": _write_binary_stl(tmp_path_factory.mktemp("moderate") / "rippled.stl", corners), "triangles": len(corners)}


@pytest.fixture(scope="module")
def quarter(dense, tmp_path_factory) -> dict:
    """The dense sphere, each format, simplified to a quarter once."""
    folder = tmp_path_factory.mktemp("quarter")
    done = {}
    for kind in ("binary", "ascii"):
        before = _sha(dense[kind])
        reply = _simplify(dense[kind], 0.25, folder / f"{kind}_quarter.stl")
        done[kind] = {"reply": reply, "sha_before": before}
    return done


# ---------------------------------------------------------------------------
# A dense closed part
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["binary", "ascii"])
class TestAQuarterOfADenseClosedPart:
    """Count, closure, size, volume and the measured movement, in both STL kinds."""

    def test_the_count_is_the_one_asked_for(self, dense, quarter, kind) -> None:
        reply = quarter[kind]["reply"]
        asked = round(dense["triangles"] * 0.25)
        assert abs(len(_read_back(reply["path"]).faces) - asked) <= _COUNT_SHARE * asked
        assert abs(reply["simplified_triangles"] - asked) <= _COUNT_SHARE * asked
        assert reply.get("success") is True, reply
        assert reply["asked_triangles"] == asked
        assert _sha(dense[kind]) == quarter[kind]["sha_before"]

    def test_the_part_stays_closed(self, quarter, kind) -> None:
        reply = quarter[kind]["reply"]
        assert _read_back(reply["path"]).is_watertight
        assert reply["watertight_before"] is True
        assert reply["watertight_after"] is True

    def test_its_size_and_volume_hold(self, dense, quarter, kind) -> None:
        reply = quarter[kind]["reply"]
        before, after = _read_back(dense["binary"]), _read_back(reply["path"])
        moved = float(np.abs(after.bounds - before.bounds).max())
        assert moved <= max(_OUTLINE_MM, 0.001 * float(before.extents.max()))
        assert abs(after.volume - before.volume) <= _VOLUME_SHARE * before.volume
        assert reply["bounding_box_change_mm"] == pytest.approx(moved, abs=1e-3)
        assert reply["volume_change_pct"] == pytest.approx((after.volume - before.volume) / before.volume * 100, abs=0.01)

    def test_the_reply_measures_how_far_the_surface_moved(self, quarter, kind) -> None:
        reply = quarter[kind]["reply"]
        assert 0.0 < reply["max_deviation_mm"] < _QUARTER_DEVIATION_MM
        assert reply["deviation_samples"] >= 1_000


class TestTheMeasuredMovementIsAMeasurement:
    """The reported movement against an independent exact measure of input corners."""

    def test_it_is_no_smaller_than_a_corner_moved_and_not_inflated(self, dense, quarter) -> None:
        scipy_spatial = pytest.importorskip("scipy.spatial")
        reply = quarter["binary"]["reply"]
        before, after = _read_back(dense["binary"]), _read_back(reply["path"])
        corners = np.asarray(before.vertices)
        picked = corners[np.random.default_rng(3).choice(len(corners), size=20_000, replace=False)]
        moved = _exact_distances(picked, after, scipy_spatial).max()
        assert moved - 1e-6 <= reply["max_deviation_mm"] <= 2.0 * moved


def _exact_distances(points: np.ndarray, mesh: trimesh.Trimesh, scipy_spatial) -> np.ndarray:
    """Distance from each point to *mesh*: candidates from a tree of
    triangle centres, wide enough to hold the nearest, then trimesh's own
    closest point on each."""
    triangles = np.asarray(mesh.triangles)
    centres = triangles.mean(axis=1)
    reach = np.linalg.norm(triangles - centres[:, None, :], axis=2).max()
    upper, _ = scipy_spatial.cKDTree(np.asarray(mesh.vertices)).query(points)
    tree = scipy_spatial.cKDTree(centres)
    out = np.empty(len(points))
    for start in range(0, len(points), 4096):
        chunk = points[start:start + 4096]
        lists = tree.query_ball_point(chunk, upper[start:start + 4096] + reach)
        owner = np.repeat(np.arange(len(chunk)), [len(found) for found in lists])
        found = np.concatenate([np.asarray(f, dtype=np.int64) for f in lists])
        closest = trimesh.triangles.closest_point(triangles[found], chunk[owner])
        best = np.full(len(chunk), np.inf)
        np.minimum.at(best, owner, np.linalg.norm(closest - chunk[owner], axis=1))
        out[start:start + len(chunk)] = best
    return out


class TestGentleAndAggressiveRatios:
    """A tenth removed and nineteen twentieths removed."""

    @pytest.mark.parametrize("ratio", [0.9, 0.05])
    def test_the_count_is_honoured_and_the_part_stays_closed(self, moderate, tmp_path, ratio) -> None:
        before = _sha(moderate["binary"])
        reply = _simplify(moderate["binary"], ratio, tmp_path / "out.stl")
        asked = round(moderate["triangles"] * ratio)
        result = _read_back(tmp_path / "out.stl")
        assert abs(len(result.faces) - asked) <= _COUNT_SHARE * asked
        assert result.is_watertight
        assert reply.get("success") is True, reply
        assert abs(reply["simplified_triangles"] - asked) <= _COUNT_SHARE * asked
        assert reply["watertight_after"] is True
        assert _sha(moderate["binary"]) == before


# ---------------------------------------------------------------------------
# What is refused, and what is left alone
# ---------------------------------------------------------------------------


class TestRefusals:
    """A part with no triangles to spare; a 3MF whose colors or objects an STL cannot carry."""

    def test_a_part_with_no_triangles_to_spare_is_refused_and_left_alone(self, tmp_path) -> None:
        cube = _write_binary_stl(tmp_path / "cube.stl", _box())
        before = _sha(cube)
        out = tmp_path / "cube_out.stl"
        reply = _simplify(cube, 0.5, out)
        assert reply.get("success") is False, reply
        assert reply["code"] == "EDIT_WOULD_DAMAGE"
        assert "unchanged" in reply["message"]
        assert not out.exists()
        assert _sha(cube) == before
        assert not list(tmp_path.glob("*.partial"))

    def test_a_3mf_with_two_colors_is_refused(self, tmp_path) -> None:
        part = _write_3mf(tmp_path / "two_colors.3mf", [_rippled_sphere(3)], two_colors=True)
        before = _sha(part)
        out = tmp_path / "out.stl"
        reply = _simplify(part, 0.5, out)
        assert reply.get("success") is False, reply
        assert "2 colors" in reply["message"]
        assert not out.exists()
        assert _sha(part) == before

    def test_a_3mf_with_two_objects_is_refused(self, tmp_path) -> None:
        second = _rippled_sphere(3) + np.float32(60.0)
        part = _write_3mf(tmp_path / "two_objects.3mf", [_rippled_sphere(3), second])
        out = tmp_path / "out.stl"
        reply = _simplify(part, 0.5, out)
        assert reply.get("success") is False, reply
        assert "2 objects" in reply["message"]
        assert not out.exists()

    def test_a_one_color_3mf_is_simplified_and_says_the_result_is_an_stl(self, tmp_path) -> None:
        corners = _rippled_sphere(4)
        part = _write_3mf(tmp_path / "one.3mf", [corners])
        reply = _simplify(part, 0.5, tmp_path / "out.stl")
        assert reply.get("success") is True, reply
        assert abs(reply["simplified_triangles"] - len(corners) // 2) <= _COUNT_SHARE * len(corners) // 2
        assert "binary STL" in reply["format_note"]
        assert _read_back(reply["path"]).is_watertight


class TestAnOpenPart:
    """A sphere with a hole: simplified without opening it further, never called closed."""

    def test_it_is_not_made_worse(self, tmp_path) -> None:
        corners = _rippled_sphere(6)
        holed = corners[corners[:, :, 2].mean(axis=1) < 0.95 * _RADIUS_MM]
        part = _write_binary_stl(tmp_path / "holed.stl", holed)
        reply = _simplify(part, 0.25, tmp_path / "out.stl")
        assert reply.get("success") is True, reply
        assert reply["watertight_before"] is False
        assert reply["watertight_after"] is False
        assert reply["open_edges_after"] <= reply["open_edges_before"]
        result = _read_back(tmp_path / "out.stl")
        assert not result.is_watertight
        assert _open_edges(result) <= _open_edges(_read_back(part))


class TestAPartAlreadyAtTheCount:
    """Nothing to take away: the file comes back as it was."""

    def test_a_tetrahedron_comes_back_unchanged(self, tmp_path) -> None:
        part = _write_binary_stl(tmp_path / "tetra.stl", _tetrahedron())
        before = _sha(part)
        reply = _simplify(part, 0.5)
        assert reply.get("success") is True, reply
        assert reply["path"] == str(part)
        assert reply["simplified_triangles"] == reply["original_triangles"] == 4
        assert "Nothing to simplify" in reply["note"]
        assert _sha(part) == before
        assert not (tmp_path / "tetra_simplified.stl").exists()


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def mesh_tools(monkeypatch) -> dict:
    tools: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def register(fn):
                tools[fn.__name__] = fn
                return fn

            return register

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    from kiln.plugins.mesh_tools import plugin

    plugin.register(_MCP())
    return tools


class TestTheSimplifyTool:
    """simplify_mesh_model through the plugin, as an agent calls it."""

    def test_the_reply_carries_the_measured_facts(self, mesh_tools, moderate, tmp_path) -> None:
        part = tmp_path / "part.stl"
        part.write_bytes(moderate["binary"].read_bytes())
        with patch("kiln.server._check_auth", return_value=None):
            result = mesh_tools["simplify_mesh_model"](file_path=str(part), target_ratio=0.5)
        asked = round(moderate["triangles"] * 0.5)
        assert result["success"] is True, result
        assert abs(result["simplified_triangles"] - asked) <= _COUNT_SHARE * asked
        assert result["watertight_before"] is True and result["watertight_after"] is True
        assert 0.0 < result["max_deviation_mm"] < _QUARTER_DEVIATION_MM
        assert result["deviation_samples"] > 0
        assert result["bounding_box_change_mm"] <= _OUTLINE_MM
        assert abs(result["volume_change_pct"]) <= _VOLUME_SHARE * 100
        assert Path(result["path"]).name == "part_simplified.stl"

    def test_a_refusal_reaches_the_caller_as_an_error(self, mesh_tools, tmp_path) -> None:
        cube = _write_binary_stl(tmp_path / "cube.stl", _box())
        with patch("kiln.server._check_auth", return_value=None):
            result = mesh_tools["simplify_mesh_model"](file_path=str(cube), target_ratio=0.5)
        assert result["success"] is False
        assert result["error"]["code"] == "EDIT_WOULD_DAMAGE"
        assert "unchanged" in result["error"]["message"]
        assert not (tmp_path / "cube_simplified.stl").exists()


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


def _brute_distances(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Distance from each point to the nearest of *triangles*, every pair
    measured: the nearer of the plane (when the point's foot lands inside)
    and the three edges."""
    out = np.full(len(points), np.inf)
    for tri in triangles:
        a, b, c = tri
        best = np.minimum(np.minimum(_to_segment(points, a, b), _to_segment(points, b, c)), _to_segment(points, c, a))
        normal = np.cross(b - a, c - a)
        size = np.linalg.norm(normal)
        if size > 1e-12:
            normal = normal / size
            height = (points - a) @ normal
            foot = points - height[:, None] * normal
            inside = np.ones(len(points), dtype=bool)
            for p, q in ((a, b), (b, c), (c, a)):
                inside &= np.cross(q - p, foot - p) @ normal >= -1e-12
            best = np.where(inside, np.minimum(best, np.abs(height)), best)
        out = np.minimum(out, best)
    return out


def _to_segment(points: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    span = b - a
    length = float(span @ span)
    t = np.clip(((points - a) @ span) / length, 0.0, 1.0) if length > 0 else np.zeros(len(points))
    return np.linalg.norm(points - (a + t[:, None] * span), axis=1)


class TestNearestSurfaceDistances:
    """Exact against a brute-force measure, for small, large, thin and far-off cases."""

    def test_every_distance_matches_a_brute_force_measure(self) -> None:
        from kiln.surface_deviation import nearest_surface_distances

        rng = np.random.default_rng(7)
        small = rng.random((300, 3, 3)) * 2.0 + rng.random((300, 1, 3)) * 40.0
        large = rng.random((6, 3, 3)) * 60.0 - 10.0
        thin = rng.random((20, 3, 3)) * np.array([50.0, 0.3, 0.3])
        line = np.array([[[0.0, 0.0, 0.0], [5.0, 5.0, 0.0], [10.0, 10.0, 0.0]]])
        triangles = np.concatenate([small, large, thin, line])
        vertices = triangles.reshape(-1, 3)
        faces = np.arange(len(vertices)).reshape(-1, 3)
        points = np.concatenate([rng.random((400, 3)) * 50.0, rng.random((40, 3)) * 400.0 - 200.0])
        measured = nearest_surface_distances(points, vertices, faces)
        np.testing.assert_allclose(measured, _brute_distances(points, triangles), rtol=0, atol=1e-9)

    def test_a_triangle_with_two_corners_at_one_place_is_left_out(self) -> None:
        from kiln.surface_deviation import nearest_surface_distances

        vertices = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [50.0, 50.0, 50.0], [51.0, 50.0, 50.0], [50.0, 51.0, 50.0]])
        measured = nearest_surface_distances(np.array([[5.0, 0.0, 0.0]]), vertices, np.array([[0, 1, 2], [3, 4, 5]]))
        assert measured[0] == pytest.approx(float(np.linalg.norm([45.0, 50.0, 50.0])), abs=1e-9)

    def test_the_same_two_surfaces_give_the_same_answer(self) -> None:
        from kiln.surface_deviation import surface_deviation

        sphere = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        moved = np.asarray(sphere.vertices) * 1.01
        first = surface_deviation(sphere.vertices, sphere.faces, moved, sphere.faces)
        second = surface_deviation(sphere.vertices, sphere.faces, moved, sphere.faces)
        assert first == second
        assert first.max_mm == pytest.approx(0.1, abs=0.01)


# ---------------------------------------------------------------------------
# The package that does the collapsing
# ---------------------------------------------------------------------------


class TestTheSimplifierPackage:
    """Declared, locked, and importable on a plain install."""

    def test_it_is_declared_locked_and_imports(self) -> None:
        import tomllib

        project = tomllib.loads((_KILN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        assert any(req.lower().replace("_", "-").startswith("fast-simplification>=") for req in project["dependencies"])
        lock = (_KILN_ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
        assert any(line.lower().replace("_", "-").startswith("fast-simplification==") for line in lock)
        import fast_simplification

        assert callable(fast_simplification.simplify)
