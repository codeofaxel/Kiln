"""The clearance between the bodies of one mesh, as numbers per pair.

Without it, the only way to read the gap between a planet and its ring in a
print-in-place gearbox, or to see that two bodies overlap and would print
fused, is to measure outside Kiln: the fusion check names a pair only when
it is a near-miss, and merges bodies that sink into one another without
saying by how much.

``measure_body_pairs`` states, for each pair nearest first, the smallest gap
and any shared volume; ``analyze_mesh_geometry`` carries it as
``body_pairs`` for a mesh of several bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from kiln.fusion_check import measure_body_pairs  # noqa: E402


def _box(size: float, center: tuple[float, float, float]) -> Any:
    box = trimesh.creation.box(extents=[size, size, size])
    box.apply_translation(center)
    return box


def _write(tmp_path: Path, *meshes: Any, name: str = "part.stl") -> str:
    path = tmp_path / name
    trimesh.util.concatenate(list(meshes)).export(path)
    return str(path)


def _pair_near(table: dict[str, Any], a_center, b_center) -> dict[str, Any]:
    """The table's row for the two bodies centred nearest the points given."""
    def body_at(point) -> int:
        return min(
            table["bodies"],
            key=lambda b: sum((c - p) ** 2 for c, p in zip(b["center_mm"], point, strict=True)),
        )["body"]

    wanted = sorted([body_at(a_center), body_at(b_center)])
    return next(row for row in table["pairs"] if sorted(row["bodies"]) == wanted)


def test_a_gap_and_an_overlap_are_each_stated(tmp_path):
    a, b, c = (5, 5, 5), (16.5, 5, 5), (5, 13, 5)  # a-b 1.5 mm apart; a-c share 2x10x10
    table = measure_body_pairs(_write(tmp_path, _box(10, a), _box(10, b), _box(10, c)))

    assert table["checked"] is True and len(table["bodies"]) == 3
    apart = _pair_near(table, a, b)
    assert apart["measured"] == "exact"
    assert apart["gap_mm"] == pytest.approx(1.5, abs=1e-3)
    assert apart["overlap_mm3"] == 0.0
    fused = _pair_near(table, a, c)
    assert fused["gap_mm"] == 0.0
    assert fused["overlap_mm3"] == pytest.approx(200.0, rel=1e-3)


def test_a_planet_in_its_ring_reads_its_clearance(tmp_path):
    ring = trimesh.creation.annulus(r_min=10.4, r_max=14.0, height=5.0, sections=256)
    planet = trimesh.creation.cylinder(radius=10.0, height=5.0, sections=256)
    table = measure_body_pairs(_write(tmp_path, ring, planet))

    (row,) = table["pairs"]
    assert row["measured"] == "exact"
    assert row["gap_mm"] == pytest.approx(0.4, abs=0.005)


def test_pairs_come_nearest_first_and_the_budget_is_said(tmp_path):
    cubes = [_box(1.0, (2.5 * i, 0.0, 0.0)) for i in range(10)]  # neighbours 1.5 mm apart
    table = measure_body_pairs(_write(tmp_path, *cubes), max_pairs=5)

    assert table["pairs_total"] == 45 and table["pairs_measured"] == 5
    gaps = [row["gap_mm"] if row["gap_mm"] is not None else float("inf") for row in table["pairs"]]
    assert gaps == sorted(gaps)
    assert gaps[0] == pytest.approx(1.5, abs=1e-3)


def test_a_pair_beyond_the_search_reads_at_least(tmp_path):
    table = measure_body_pairs(_write(tmp_path, _box(1.0, (0, 0, 0)), _box(1.0, (50, 0, 0))))

    (row,) = table["pairs"]
    assert row["gap_mm"] is None and row["gap_at_least_mm"] == 2.0


def test_an_open_body_is_measured_by_sampling_and_says_so(tmp_path):
    box = _box(10, (5, 5, 5))
    open_box = trimesh.Trimesh(box.vertices, box.faces[:-2], process=False)  # one side left open
    table = measure_body_pairs(_write(tmp_path, open_box, _box(10, (16.5, 5, 5))))

    (row,) = table["pairs"]
    assert row["measured"] == "sampled"
    assert row["overlap_mm3"] is None
    assert row["gap_mm"] == pytest.approx(1.5, abs=0.05)


def test_one_body_has_no_table(tmp_path):
    table = measure_body_pairs(_write(tmp_path, _box(10, (0, 0, 0))))
    assert table == {"checked": False, "reason": "the mesh is one body"}


def test_the_tool_carries_the_table_for_several_bodies_only(tmp_path):
    from kiln.plugins.mesh_tools import _MeshToolsPlugin

    class _MCP:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self, **_kwargs):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn

            return deco

    mcp = _MCP()
    _MeshToolsPlugin().register(mcp)
    analyze = mcp.tools["analyze_mesh_geometry"]

    two = analyze(_write(tmp_path, _box(10, (5, 5, 5)), _box(10, (16.5, 5, 5)), name="two.stl"))
    one = analyze(_write(tmp_path, _box(10, (5, 5, 5)), name="one.stl"))

    assert two["success"] is True
    assert two["body_pairs"]["pairs"][0]["gap_mm"] == pytest.approx(1.5, abs=1e-3)
    assert one["success"] is True and "body_pairs" not in one


def test_a_lattice_of_many_bodies_is_not_tabled(tmp_path):
    """A pair table grows with the square of the bodies; past 64 it says so."""
    cubes = [_box(1.0, (2.0 * (i % 13), 2.0 * (i // 13), 0.0)) for i in range(65)]
    table = measure_body_pairs(_write(tmp_path, *cubes))

    assert table["checked"] is False
    assert "65 bodies" in table["reason"]
