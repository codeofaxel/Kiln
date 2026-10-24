"""Pieces that share a surface or sink into one another print as one solid.

A file from Blender or any other modeller is often several pieces: a donut,
its icing laid on it, sprinkles sunk in the icing.  A slicer cuts every
triangle of the file into one outline per layer, so those pieces print as
one part, while two meeting only along an edge print as two.  Until then every door read "more than one piece" as a
defect: the validation passes failed the file as not closed, the diagnosis
called the icing and sprinkles floating fragments and advised keeping only
the largest piece, the fusion check split the donut into 1,465 bodies and
called 1,052 of them scrap, and ``remove_floating_regions`` deleted every
sprinkle when asked to tidy the file.

These tests hold every door to the one reading (:mod:`kiln.mesh_bodies`):
which pieces print as one solid, and which piece really floats.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _cylinder(radius, height, at, *, lying=False):
    c = trimesh.creation.cylinder(radius=radius, height=height, sections=48)
    if lying:
        c.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    c.apply_translation(at)
    return c


def _hollow():
    inner = _box((-7, -7, 3), (7, 7, 17))
    inner.invert()
    return [_box((-10, -10, 0), (10, 10, 20)), inner]


def _ball(z):
    ball = trimesh.creation.icosphere(subdivisions=2, radius=3.0)
    ball.apply_translation((0, 0, z))
    return ball


#: Each scene, and how many solids a slicer prints it as.
SCENES = {
    "share_a_side_face": ([_box((0, 0, 0), (20, 20, 10)), _box((20, 0, 0), (40, 20, 10))], 1),
    "stacked": ([_box((0, 0, 0), (20, 20, 10)), _box((0, 0, 10), (20, 20, 20))], 1),
    "coat_triangulated_its_own_way": (
        [_box((0, 0, 0), (20, 20, 10)), _box((0, 0, 10), (20, 20, 12)).subdivide().subdivide()], 1,
    ),
    "overlap": ([_box((0, 0, 0), (20, 20, 10)), _box((15, 0, 0), (35, 20, 10))], 1),
    "embedded": ([_box((0, 0, 0), (40, 40, 10)), _box((10, 10, 2), (20, 20, 8))], 1),
    "hollow_part": (_hollow(), 1),
    "ball_sunk_into_the_cavity_floor": ([*_hollow(), _ball(5.0)], 1),
    "meet_along_one_edge": ([_box((0, 0, 0), (20, 20, 10)), _box((20, 20, 0), (40, 40, 10))], 2),
    "standing_tangent_against_a_side": ([_box((0, 0, 0), (40, 20, 10)), _cylinder(5, 10, (20, 25, 5))], 2),
    "apart": ([_box((0, 0, 0), (20, 20, 10)), _box((25, 0, 0), (45, 20, 10))], 2),
    "hinge_clearance": ([_box((0, 0, 0), (20, 20, 10)), _box((20.2, 0, 0), (40, 20, 10))], 2),
    "loose_ball_in_a_hollow_part": ([*_hollow(), _ball(10.0)], 2),
}


@pytest.mark.parametrize("scale", [1.0, 0.001, 25.4], ids=["mm", "m", "in"])
@pytest.mark.parametrize("scene", list(SCENES))
def test_the_reading_groups_pieces_into_the_solids_a_slicer_prints(scene, scale):
    from kiln.mesh_bodies import read_triangles

    parts, solids = SCENES[scene]
    reading = read_triangles(np.asarray(trimesh.util.concatenate(parts).triangles) * scale)
    assert reading.piece_count == len(parts)
    assert reading.solid_count == solids, reading.joined


def _cubes(cells):
    out = []
    for x, y, z in cells:
        c = trimesh.creation.box(extents=(1, 1, 1))
        c.apply_translation((x + 0.5, y + 0.5, z + 0.5))
        out.append(c)
    return out


def test_a_lattice_of_thousands_of_face_sharing_cells_is_one_solid():
    """A crowded scene stays inside the testing budget: a pair already in one
    solid is never tested (a first version ran out of budget and read 2,000
    face-sharing cubes as 1,524 solids)."""
    from kiln.mesh_bodies import read_triangles

    lattice = _cubes((x, y, z) for x in range(20) for y in range(20) for z in range(5))
    reading = read_triangles(np.asarray(trimesh.util.concatenate(lattice).triangles))
    assert (reading.piece_count, reading.solid_count, reading.untested_pairs) == (2000, 1, 0)


def test_cells_meeting_only_at_their_edges_stay_apart():
    from kiln.mesh_bodies import read_triangles

    board = _cubes((x, y, 0) for x in range(30) for y in range(30) if (x + y) % 2 == 0)
    reading = read_triangles(np.asarray(trimesh.util.concatenate(board).triangles))
    assert reading.solid_count == reading.piece_count == 450


def test_spheres_sunk_a_tenth_of_a_millimetre_into_a_plate_are_one_solid():
    """The probes are spread over every triangle of a piece: a first version
    took the first ones in file order and missed a sphere's underside."""
    from kiln.mesh_bodies import read_triangles

    spheres = []
    for i in range(300):
        s = trimesh.creation.icosphere(subdivisions=1, radius=0.4)
        s.apply_translation((i % 20, i // 20, 0.4))
        spheres.append(s)
    reading = read_triangles(np.asarray(
        trimesh.util.concatenate([*spheres, _box((-5, -5, -1.9), (25, 20, 0.1))]).triangles,
    ))
    assert (reading.piece_count, reading.solid_count) == (301, 1)


def test_rods_that_all_span_one_axis_are_swept_along_another(monkeypatch):
    """Pieces that all overlap along one axis would meet each other in a
    sweep along it (two thousand rods: about two million looks); the sweep
    takes the axis they are spread out on (about eighty thousand)."""
    import kiln.mesh_bodies as mb
    from kiln.mesh_bodies import read_triangles

    monkeypatch.setattr(mb, "_MAX_SWEEP_LOOKS", 200_000)
    rods = []
    for i in range(2000):
        rod = _box((0, 0, 0), (100, 0.2, 0.2))
        rod.apply_translation((0, (i % 50) * 0.5, (i // 50) * 0.5))
        rods.append(rod)
    reading = read_triangles(np.asarray(trimesh.util.concatenate(rods).triangles))
    assert (reading.solid_count, reading.untested_pairs) == (2000, 0)


def test_the_search_stops_at_its_budget_and_says_what_it_left(monkeypatch):
    """A server reads files people upload, so the work the contact search
    does is capped; what it leaves reads as apart and is counted, never
    guessed."""
    import kiln.mesh_bodies as mb

    spheres = []
    for i in range(40):
        s = trimesh.creation.icosphere(subdivisions=1, radius=0.4)
        s.apply_translation((i % 8, i // 8, 0.4))
        spheres.append(s)
    scene = np.asarray(trimesh.util.concatenate([*spheres, _box((-2, -2, -1.9), (10, 8, 0.1))]).triangles)
    assert mb.read_triangles(scene).solid_count == 1

    monkeypatch.setattr(mb, "_MAX_CONTACT_WORK", 1)
    starved = mb.read_triangles(scene)
    assert starved.solid_count == 41 and starved.untested_pairs == 40
    assert "40 pairs of pieces were not checked" in starved.solids_sentence()

    monkeypatch.setattr(mb, "_MAX_CONTACT_WORK", 10**12)
    monkeypatch.setattr(mb, "_MAX_SWEEP_LOOKS", 0)
    assert mb.read_triangles(scene).untested_pairs > 0


def _filament_mm(slicer: str, stl: Path) -> float:
    out = stl.with_suffix(".gcode")
    subprocess.run(
        [slicer, "--export-gcode", "--dont-arrange", "--layer-height", "0.2", "--perimeters", "2",
         "--fill-density", "15%", "--skirts", "0", "--output", str(out), str(stl)],
        capture_output=True, check=True, timeout=300,
    )
    return float(re.search(r"filament used \[mm\] = ([0-9.]+)", out.read_text()).group(1))


def _prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    mac = Path("/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer")
    return found or (str(mac) if mac.exists() else None)


@pytest.mark.skipif(_prusaslicer() is None, reason="PrusaSlicer is not installed")
def test_the_premise_a_slicer_prints_a_shared_face_as_one_solid_and_a_shared_edge_as_two(tmp_path):
    """What the reading is grouped by, measured in the slicer: pieces sharing
    a face use the filament of one solid; pieces meeting along an edge use
    the filament of each piece sliced on its own."""
    slicer = _prusaslicer()

    def stl(name, parts):
        mesh = trimesh.util.concatenate(parts)
        mesh.apply_translation((100, 100, 0))
        path = tmp_path / f"{name}.stl"
        mesh.export(path)
        return path

    one = _filament_mm(slicer, stl("one", [_box((0, 0, 0), (40, 20, 10))]))
    face = _filament_mm(slicer, stl("face", SCENES["share_a_side_face"][0]))
    edge_parts = SCENES["meet_along_one_edge"][0]
    edge = _filament_mm(slicer, stl("edge", edge_parts))
    alone = sum(_filament_mm(slicer, stl(f"alone{i}", [p])) for i, p in enumerate(edge_parts))
    assert face == pytest.approx(one, abs=0.5)
    assert edge == pytest.approx(alone, abs=1.0)
    assert edge > face + 50.0


# ---------------------------------------------------------------------------
# Every door, on a coat with three sprinkles sunk in it
# ---------------------------------------------------------------------------


def _coated(tmp_path: Path, *, with_a_loose_piece: bool = False) -> Path:
    """A block on the bed, a coat laid on its top face, three sprinkles sunk
    half a millimetre into the coat: five pieces, one solid.  Optionally a
    sixth piece 15 mm up in the air, joined to nothing."""
    parts = [_box((0, 0, 0), (30, 30, 10)), _box((0, 0, 10), (30, 30, 12))]
    parts += [_cylinder(1.0, 8.0, (x, 15, 12.5), lying=True) for x in (8, 15, 22)]
    if with_a_loose_piece:
        parts.append(_box((40, 0, 15), (44, 4, 19)))
    path = tmp_path / ("coated_and_loose.stl" if with_a_loose_piece else "coated.stl")
    trimesh.util.concatenate(parts).export(path)
    return path


def _call(name: str, **args):
    from kiln import server
    from tests import _real_exports as rx

    return rx.call(server.mcp, name, args)


def test_the_validation_check_calls_touching_pieces_closed_and_says_they_touch(tmp_path):
    from kiln.generation.validation import validate_mesh

    result = validate_mesh(str(_coated(tmp_path)))
    assert result.is_manifold is True
    assert not any("not manifold" in w for w in result.warnings), result.warnings
    said = " ".join(result.notes)
    assert "sit on each other" in said and "needs no repair" in said
    assert "print as one solid" in said


def test_validate_and_prepare_mesh_passes_them(tmp_path):
    reply = _call("validate_and_prepare_mesh", file_path=str(_coated(tmp_path)),
                  build_volume_x=256, build_volume_y=256, build_volume_z=256)
    assert reply["passed"] is True, reply["message"]
    assert reply["result"]["is_manifold"] is True
    assert reply["result"]["was_repaired"] is False
    assert "not closed" not in reply["message"] and "non-manifold" not in reply["message"]
    assert "print as one solid" in reply["message"]


def test_the_pre_print_check_calls_every_piece_closed(tmp_path):
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    report = run_full_validation_pipeline(str(_coated(tmp_path)))
    watertight = next(c for c in report["checks"] if c["name"] == "watertight")
    assert watertight["passed"] is True and "sit on each other" in watertight["details"]
    assert not report["repaired"]


def test_the_diagnosis_finds_no_scrap_and_no_unfused_attachment(tmp_path):
    from kiln.mesh_diagnostics import diagnose_mesh

    report = diagnose_mesh(str(_coated(tmp_path)))
    assert report.component_count == 5
    assert report.has_floating_fragments is False
    assert report.detached_attachment is False
    assert not any("fragment" in d or "never fused" in d for d in report.defects), report.defects
    assert not any("largest component" in r for r in report.recommendations)
    assert any("print as one solid" in n for n in report.notes)


def test_the_diagnosis_still_finds_a_piece_that_really_floats(tmp_path):
    from kiln.mesh_diagnostics import diagnose_mesh

    report = diagnose_mesh(str(_coated(tmp_path, with_a_loose_piece=True)))
    assert report.has_floating_fragments is True
    assert any("1 floating fragment" in d for d in report.defects), report.defects


def test_the_fusion_check_reads_one_solid(tmp_path):
    from kiln.fusion_check import check_fusion

    report = check_fusion(str(_coated(tmp_path)))
    assert (report["body_count"], report["solid_count"]) == (5, 1)
    assert report["floating_bodies"] == 0 and report["findings"] == []


def test_tidying_a_file_never_deletes_a_piece_of_the_part(tmp_path):
    from kiln.generation.validation import read_stl_triangles, remove_floating_regions

    coated = _coated(tmp_path)
    before = len(read_stl_triangles(coated))
    result = remove_floating_regions(str(coated), output_path=str(tmp_path / "out.stl"))
    assert result["removed_triangles"] == 0, result
    assert len(read_stl_triangles(tmp_path / "out.stl")) == before


def test_removing_what_floats_removes_only_the_loose_piece(tmp_path):
    from kiln.generation.validation import read_stl_triangles, remove_floating_regions

    loose = _coated(tmp_path, with_a_loose_piece=True)
    part = len(read_stl_triangles(_coated(tmp_path)))
    result = remove_floating_regions(str(loose), output_path=str(tmp_path / "out.stl"), floating_only=True)
    assert result["removed_components"] == 1
    assert len(read_stl_triangles(tmp_path / "out.stl")) == part


def test_auto_fix_removes_only_what_floats(tmp_path):
    from kiln.generation.validation import can_print_now, read_stl_triangles

    part = len(read_stl_triangles(_coated(tmp_path)))
    clean = can_print_now(str(_coated(tmp_path)), auto_fix=True, output_path=str(tmp_path / "a.stl"))
    assert not any(i["type"] == "floating_regions" for i in clean["issues"]), clean["issues"]
    assert not any("floating" in a for a in clean["actions_taken"])
    assert len(read_stl_triangles(tmp_path / "a.stl")) == part

    loose = can_print_now(str(_coated(tmp_path, with_a_loose_piece=True)), auto_fix=True,
                          output_path=str(tmp_path / "b.stl"))
    assert any("Removed 1 floating" in a for a in loose["actions_taken"]), loose["actions_taken"]
    assert len(read_stl_triangles(tmp_path / "b.stl")) == part


def test_the_quick_mesh_check_counts_solids(tmp_path):
    from kiln.generation.validation import analyze_mesh, predict_print_failures

    analysis = analyze_mesh(str(_coated(tmp_path)))
    assert (analysis.connected_components, analysis.solid_count) == (5, 1)
    assert not any("separate solids" in i or "floating" in i for i in analysis.printability_issues)
    failures = predict_print_failures(str(_coated(tmp_path)))
    found = failures.get("failures", failures) if isinstance(failures, dict) else failures
    assert not any(f.get("type") == "disconnected_parts" for f in found)
