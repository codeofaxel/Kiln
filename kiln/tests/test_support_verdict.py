"""One answer to "does this part need supports?", at every door.

2026-10-19: Kiln had three.  ``printability.analyze_printability`` said a
part needed supports when any face past the threshold hung in the air and
no bridge or sideways reach saved it; ``support_assessment`` said so only
when more than 5 % of the surface AREA overhung; the CLI and the slice
chokepoint added a floor of 1 % of the triangles on top of the first.  A
desk stand with a 70 mm cantilevered arm came back "needs supports" from
the report, "no supports" from the pre-print check, and -- had the stand
been a finely meshed base with a small arm -- "no supports" from the slice.
Now every door reads :func:`kiln.printability.support_verdict`; this file
feeds the same meshes to each of them and asserts one answer.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from kiln import slicer_supports

_Tri = tuple[tuple[float, float, float], ...]


def _box(x0: float, y0: float, z0: float, x: float, y: float, z: float) -> list[_Tri]:
    v = [(x0, y0, z0), (x0 + x, y0, z0), (x0 + x, y0 + y, z0), (x0, y0 + y, z0),
         (x0, y0, z0 + z), (x0 + x, y0, z0 + z), (x0 + x, y0 + y, z0 + z), (x0, y0 + y, z0 + z)]
    f = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [tuple(v[i] for i in face) for face in f]


def _cylinder(cx: float, cy: float, z0: float, r: float, h: float, n: int = 400) -> list[_Tri]:
    """A finely meshed disc: ``4 * n`` faces, so a small feature beside it
    is a tiny share of the triangles."""
    f: list[_Tri] = []
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        f.append(((p0[0], p0[1], z0), (cx, cy, z0), (p1[0], p1[1], z0)))
        f.append(((p0[0], p0[1], z0 + h), (p1[0], p1[1], z0 + h), (cx, cy, z0 + h)))
        f.append(((p0[0], p0[1], z0), (p1[0], p1[1], z0), (p0[0], p0[1], z0 + h)))
        f.append(((p1[0], p1[1], z0), (p1[0], p1[1], z0 + h), (p0[0], p0[1], z0 + h)))
    return f


def _write_stl(path: Path, facets: list[_Tri]) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(facets)))
        for tri in facets:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for vertex in tri:
                fh.write(struct.pack("<3f", *vertex))
            fh.write(struct.pack("<H", 0))
    return str(path)


#: (name, faces, needs supports) -- the shapes that told the three rules apart.
CASES: list[tuple[str, list[_Tri], bool]] = [
    ("box", _box(0, 0, 0, 20, 20, 10), False),
    # A 10 mm post with 20 mm of arm each side: every rule's easy case.
    ("T_cantilever", _box(20, 0, 0, 10, 10, 30) + _box(0, 0, 30, 50, 10, 4), True),
    # The stand's shape: a big base and a small cantilever.  2.4 % of the
    # surface area and 0.1 % of the faces overhang, under both old floors.
    ("fine_base_small_cantilever",
     _cylinder(50, 50, 0, 40, 10) + _box(10, 45, 0, 6, 6, 30) + _box(0, 45, 30, 16, 6, 3), True),
    # An 8 mm slot ceiling a slicer bridges: 20 % of the area overhangs and
    # none of it needs a support.
    ("short_ceiling_slot",
     _box(0, 0, 0, 30, 20, 6) + _box(0, 0, 6, 11, 20, 6) + _box(19, 0, 6, 11, 20, 6) + _box(0, 0, 12, 30, 20, 6),
     False),
    # A 40 mm cap on a post over a 60 mm base: every overhang is above the
    # model, where plate-only supports cannot stand.
    ("cap_over_own_base", _box(0, 0, 0, 60, 60, 4) + _box(25, 25, 4, 10, 10, 30) + _box(10, 10, 34, 40, 40, 4), True),
    # Two posts 50 mm apart under a deck: a bridge too long for PLA.
    ("long_bridge", _box(0, 0, 0, 5, 10, 20) + _box(55, 0, 0, 5, 10, 20) + _box(0, 0, 20, 60, 10, 4), True),
]


@pytest.fixture(autouse=True)
def _fresh_decisions():
    slicer_supports._decisions.clear()
    yield
    slicer_supports._decisions.clear()


def _every_door(path: str, faces: list[_Tri]) -> dict[str, bool]:
    """The answer each door gives for the same part."""
    from kiln.auto_orient import estimate_supports
    from kiln.generation.validation import can_print_now, estimate_support_volume
    from kiln.printability import analyze_printability
    from kiln.support_assessment import assess_support_feasibility

    report = analyze_printability(path, material="pla")
    verdict = can_print_now(path, material="pla")
    return {
        # The report's own verdict, spelled out so a tree without
        # support_verdict still shows each door's answer beside it.
        "printability_report": bool(report.overhangs.needs_supports or report.bridging.needs_supports_for_bridges),
        "support_assessment_from_file": assess_support_feasibility(stl_path=path, material="PLA").needs_supports,
        "support_assessment_from_triangles": assess_support_feasibility(triangles=faces, material="PLA").needs_supports,
        "support_volume_estimate": estimate_support_volume(path)["needs_supports"],
        "orientation_estimate": estimate_supports(path).needs_supports,
        "pre_print_check": any(issue["type"] == "needs_supports" for issue in verdict["issues"]),
        "the_slice": slicer_supports.decide_supports(path, material="PLA").enabled,
    }


@pytest.mark.parametrize(("name", "faces", "needs"), CASES, ids=[c[0] for c in CASES])
def test_every_door_gives_the_one_answer(tmp_path: Path, name: str, faces: list[_Tri], needs: bool) -> None:
    path = _write_stl(tmp_path / f"{name}.stl", faces)
    answers = _every_door(path, faces)
    assert answers == dict.fromkeys(answers, needs), answers


def test_the_pipelines_support_step_reads_the_printability_steps_verdict(tmp_path: Path, monkeypatch) -> None:
    """The pre-print check runs both steps; the support step takes the
    verdict the printability step already made, so the two checks cannot
    disagree and the part is not read twice."""
    import kiln.support_assessment as sa
    from kiln.plugins import _validation_pipeline_internals as internals

    path = _write_stl(tmp_path / "t.stl", CASES[1][1])
    report = internals._PipelineReport(input_path=path)
    internals._step_printability(report, path, material="pla")
    assert report.model_info["support_verdict"].startswith("overhangs=")

    handed: dict[str, object] = {}
    real = sa.assess_support_feasibility

    def spy(**kwargs):
        handed.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(sa, "assess_support_feasibility", spy)
    internals._step_support_assessment(report, path, "pla")
    assert handed["needs_supports"] is True
    assert any(
        c.name == "support_assessment" and "Supports recommended" in c.details for c in report.checks
    ), [(c.name, c.details) for c in report.checks]


def test_a_support_step_run_alone_reads_the_same_engine(tmp_path: Path) -> None:
    from kiln.plugins import _validation_pipeline_internals as internals

    path = _write_stl(tmp_path / "t.stl", CASES[1][1])
    report = internals._PipelineReport(input_path=path)
    internals._step_support_assessment(report, path, "pla")
    assert any(c.name == "support_assessment" and "Supports recommended" in c.details for c in report.checks)


def test_the_slice_supports_exactly_the_faces_the_verdict_counted(tmp_path: Path) -> None:
    """The slicer's threshold is the complement of Kiln's overhang angle
    (both slicers take the slope from horizontal, Kiln from vertical), so
    the supports stand under the faces the report called overhangs and
    no others -- and a Pro per-material angle reaches the G-code the same
    way."""
    from kiln.printability import analyze_printability, overhang_threshold_deg

    path = _write_stl(tmp_path / "t.stl", CASES[1][1])
    report = analyze_printability(path, material="pla")
    assert report.overhang_threshold_deg == overhang_threshold_deg("pla")
    decision = slicer_supports.decide_supports(path, material="PLA")
    assert decision.settings["support_material_threshold"] == f"{90 - report.overhang_threshold_deg:g}"
    assert slicer_supports.support_settings(35.0)["support_material_threshold"] == "55"
    assert slicer_supports.support_settings(0.0)["support_material_threshold"] == "89"
    assert slicer_supports.support_settings(50.0)["support_material_buildplate_only"] == "0"
    assert slicer_supports.support_settings(50.0, build_plate_only=True)["support_material_buildplate_only"] == "1"


@pytest.mark.parametrize(("name", "faces", "needs"), CASES, ids=[c[0] for c in CASES])
def test_the_slices_own_reading_is_the_reports(tmp_path: Path, name: str, faces: list[_Tri], needs: bool) -> None:
    """``judge_supports`` is the support half of ``analyze_printability``
    lifted out, so the slice can read a part in a fraction of the time
    (3.6 s against 16 s on a 200,000-face part); it must give the report's
    answer, to the word."""
    from kiln.printability import analyze_printability, judge_supports, support_verdict

    path = _write_stl(tmp_path / f"{name}.stl", faces)
    judged = judge_supports(path, material="pla")
    report = analyze_printability(path, material="pla")
    assert support_verdict(judged) == support_verdict(report)
    assert (support_verdict(judged) is not None) is needs
    assert judged.overhang_threshold_deg == report.overhang_threshold_deg
    # The same block, less the score deduction only the full report sets.
    for name in ("needs_supports", "overhang_percentage", "max_overhang_angle", "overhang_triangle_count"):
        assert getattr(judged.overhangs, name) == getattr(report.overhangs, name), name
    assert judged.bridging.max_bridge_length_mm == report.bridging.max_bridge_length_mm
