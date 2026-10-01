"""One part, one printability verdict.

An 80 x 55 x 28 mm PETG enclosure (2026-09-30) got four answers that
disagreed with each other:

1. ``analyze_printability`` graded it 94/A while ``optimize_print_orientation``,
   having rotated nothing, reported ``printability_score: 80`` — a second,
   cruder scoring engine answering under the same name.
2. One ``slice_and_estimate`` result said "no brim needed" in its adhesion
   block and "Add a 5-8mm brim" in its warping block.
3. The material wall rule said a 1.20 mm wall was too thin for PETG while
   ``thin_walls`` reported nothing thin — and it named only the thinnest
   wall, so a lid ledge, four standoffs and four bosses took three rounds to
   find.
4. ``printability.cost.weight_grams`` (35.98) sat beside the slicer's 29.93 g
   with nothing saying which was which.

The fixture below rebuilds that part's shape: a 2 mm shell with a lid rebate
that leaves a 1.2 mm ledge, four PCB standoffs with 1.4 mm walls, four
insert bosses with 1.5 mm walls, and a port and vent slots whose tops are
short ceilings a slicer bridges.
"""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the fixture

ENCLOSURE_WALL_FEATURES_MM = (1.2, 1.4, 1.5)
STAND_IN_FLOOR_MM = 1.55
STANDOFF_XY = [(sx * 25.0, sy * 15.0) for sx in (-1, 1) for sy in (-1, 1)]
BOSS_XY = [(sx * 33.0, sy * 20.5) for sx in (-1, 1) for sy in (-1, 1)]


def _build_enclosure(path: Path) -> str:
    import trimesh  # noqa: F401  (registers the boolean engine)
    from trimesh.creation import box, cylinder

    length, width, height, wall = 80.0, 55.0, 28.0, 2.0

    def at(mesh, x: float, y: float, z0: float, h: float):
        mesh.apply_translation((x, y, z0 + h / 2.0))
        return mesh

    outer = at(box(extents=(length, width, height)), 0, 0, 0, height)
    inner = at(box(extents=(length - 2 * wall, width - 2 * wall, height)), 0, 0, wall, height)
    body = outer.difference(inner)
    # Lid rebate 0.8 mm into the 2 mm wall, 1.5 mm deep: a 1.2 mm ledge.
    rebate = at(
        box(extents=(length - 2 * wall + 1.6, width - 2 * wall + 1.6, 2.5)),
        0, 0, height - 1.5, 2.5,
    )
    body = body.difference(rebate)
    # PCB standoffs: 5.0 mm OD around a 2.2 mm bore -> 1.4 mm walls.
    for x, y in STANDOFF_XY:
        body = body.union(at(cylinder(radius=2.5, height=4.1, sections=48), x, y, wall - 0.1, 4.1))
        body = body.difference(at(cylinder(radius=1.1, height=4.2, sections=48), x, y, wall, 4.2))
    # Insert bosses: 7.0 mm OD around a 4.0 mm hole -> 1.5 mm walls.
    boss_height = height - 3.0 - wall + 0.1
    for x, y in BOSS_XY:
        body = body.union(at(cylinder(radius=3.5, height=boss_height, sections=48), x, y, wall - 0.1, boss_height))
        body = body.difference(at(cylinder(radius=2.0, height=6.01, sections=48), x, y, height - 9.0, 6.01))
    # A port through the end wall and six vents: short ceilings.
    body = body.difference(at(box(extents=(wall + 2.0, 9.5, 3.5)), -length / 2, 0, wall + 4.25, 3.5))
    for x in (-12.5, -7.5, -2.5, 2.5, 7.5, 12.5):
        body = body.difference(at(box(extents=(2.0, wall + 2.0, 12.0)), x, width / 2 - wall / 2, wall + 8.0, 12.0))
    assert body.is_watertight
    body.export(str(path))
    return str(path)


@pytest.fixture(scope="module")
def enclosure_stl(tmp_path_factory) -> str:
    return _build_enclosure(tmp_path_factory.mktemp("enclosure") / "enclosure.stl")


@pytest.fixture
def enclosure(enclosure_stl, tmp_path) -> str:
    """A private copy: some doors write their result over the input."""
    path = tmp_path / "enclosure.stl"
    shutil.copyfile(enclosure_stl, path)
    return str(path)


def _block_kiln_pro(monkeypatch) -> None:
    """Make every ``kiln_pro`` import fail, submodules included.

    Blocking the package alone is not enough: the suite's conftest imports
    the installed kiln-pro while it collects, and a submodule already in
    ``sys.modules`` (``kiln_pro.data_overlays``) still answers
    ``from kiln_pro.data_overlays import ...`` with its parent blocked —
    which hands a "free" run the curated overlay.
    """
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def free_tier(monkeypatch):
    """No kiln-pro in reach: the public safety floor answers everything."""
    _block_kiln_pro(monkeypatch)


def _install_pro_wall_floor(monkeypatch, floor_mm: float | None, calls: list[dict] | None = None) -> None:
    """A kiln-pro stand-in that answers only the wall-floor question.

    Its ``enrich_printability_report`` hands the report back untouched, so
    what the test sees is public Kiln's own measurement at the floor Pro
    named.
    """
    _block_kiln_pro(monkeypatch)
    overlay = types.ModuleType("kiln_pro.printability_overlay")

    def resolve_wall_floor(material, **kwargs):
        if calls is not None:
            calls.append({"material": material, **kwargs})
        return floor_mm

    overlay.resolve_wall_floor = resolve_wall_floor
    overlay.enrich_printability_report = lambda report, **_kw: dict(report)

    class _ProFeatures:
        printability_overlay = overlay

        def is_available(self, feature: str) -> bool:
            return feature == "printability_overlay"

    bridge = types.ModuleType("kiln_pro.bridge")
    bridge.pro_features = _ProFeatures()
    package = types.ModuleType("kiln_pro")
    package.bridge = bridge
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)
    monkeypatch.setitem(sys.modules, "kiln_pro.printability_overlay", overlay)


def _register(plugin_cls) -> dict[str, Any]:
    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    plugin_cls().register(_FakeMcp())
    return tools


@pytest.fixture
def no_auth(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# 1. One printability score
# ---------------------------------------------------------------------------


class TestOnePrintabilityScore:
    def test_orientation_reports_the_score_analyze_printability_gives(self, enclosure, free_tier):
        from kiln.generation.validation import optimize_orientation
        from kiln.printability import analyze_printability

        result = optimize_orientation(enclosure)
        report = analyze_printability(result["path"])

        assert (result["rotation_x_deg"], result["rotation_y_deg"]) == (0.0, 0.0)
        assert result["printability_score"] == report.score
        assert result["printability_grade"] == report.grade

    def test_the_material_the_score_was_judged_for_is_the_callers(self, enclosure, free_tier):
        from kiln.generation.validation import optimize_orientation
        from kiln.printability import analyze_printability

        result = optimize_orientation(enclosure, material="abs")
        report = analyze_printability(result["path"], material="abs")

        assert result["printability_material"] == "abs"
        assert result["printability_score"] == report.score

    def test_the_tool_doors_agree(self, enclosure, free_tier, no_auth):
        from kiln.plugins.design_reasoning_tools import _DesignReasoningToolsPlugin
        from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

        orient = _register(_DesignReasoningToolsPlugin)["optimize_print_orientation"]
        analyze = _register(_PrintabilityToolsPlugin)["analyze_printability"]

        oriented = orient(file_path=enclosure, material="petg")
        graded = analyze(file_path=oriented["path"], material="petg")

        assert oriented["success"] and graded["success"], (oriented, graded)
        assert oriented["printability_score"] == graded["report"]["score"]
        assert oriented["printability_grade"] == graded["report"]["grade"]

    def test_the_scorecard_printability_factor_is_the_printability_score(self, enclosure, free_tier):
        from kiln.generation.validation import design_scorecard
        from kiln.printability import analyze_printability

        factor = design_scorecard(enclosure, material="petg")["printability"]
        report = analyze_printability(enclosure, material="petg")

        assert (factor["score"], factor["grade"]) == (report.score, report.grade)
        assert factor["material"] == "petg"

    def test_the_quick_mesh_check_is_never_called_a_printability_score(self, enclosure, free_tier):
        from kiln.generation.validation import (
            analyze_mesh,
            can_print_now,
            compare_meshes,
            predict_print_failures,
        )
        from kiln.printability import analyze_printability

        doors = {
            "analyze_mesh": analyze_mesh(enclosure).to_dict(),
            "can_print_now": can_print_now(enclosure),
            "predict_print_failures": predict_print_failures(enclosure),
            "compare_meshes": compare_meshes(enclosure, enclosure),
        }
        expected = analyze_printability(enclosure).score
        for door, out in doors.items():
            # The one printability score a door may carry is the analyzer's.
            named = {k: v for k, v in out.items() if k.startswith("printability_score") or k == "printability_delta"}
            assert all(k == "printability_score" and v == expected for k, v in named.items()), (
                f"{door} calls something other than analyze_printability's score a printability score: {named}"
            )
            assert any(k.startswith("mesh_check_score") for k in out), f"{door} dropped the mesh check entirely"


# ---------------------------------------------------------------------------
# 2. One voice decides the brim
# ---------------------------------------------------------------------------


def _all_advice(report) -> list[str]:
    advice = list(report.recommendations)
    for block in (report.warping, report.adhesion_force):
        if block is not None:
            advice.extend(block.recommendations)
    return advice


def _speaks_of_brim(line: str) -> bool:
    return "brim" in line.lower() or "mouse-ear" in line.lower() or "mouse ear" in line.lower()


class TestOneBrimDecision:
    def test_no_other_block_prescribes_a_brim_the_decision_declined(self, enclosure, free_tier):
        from kiln.printability import analyze_printability

        report = analyze_printability(enclosure, material="petg", printer_id="bambu_a1")

        assert report.warping is not None and report.warping.risk_level == "moderate"
        decision = report.adhesion
        assert decision is not None
        assert decision.brim_width_mm == 0 and not decision.use_raft
        stray = [line for line in _all_advice(report) if _speaks_of_brim(line)]
        assert stray == [], f"brim advice beside a no-brim decision: {stray}"

    def test_a_high_warping_verdict_reaches_the_decision(self, enclosure, free_tier):
        from kiln.printability import analyze_printability

        report = analyze_printability(enclosure, material="abs", printer_id="bambu_a1")

        assert report.warping.risk_level in ("high", "critical")
        assert report.adhesion.brim_width_mm >= 5
        brim_lines = [line for line in _all_advice(report) if _speaks_of_brim(line)]
        assert len(brim_lines) == 1, brim_lines
        assert f"{report.adhesion.brim_width_mm}" in brim_lines[0]

    def test_an_adhesion_force_warning_reaches_the_decision(self, tmp_path, free_tier):
        from kiln.printability import analyze_printability

        # Full contact, so contact area alone never asked for a brim; the
        # force balance on a 4 x 4 x 200 mm tower says it will come off.
        tower = _box_stl(tmp_path / "tower.stl", 4.0, 4.0, 200.0)
        report = analyze_printability(tower, material="pla")

        assert report.bed_adhesion.adhesion_risk == "low"
        assert report.adhesion_force.risk_level == "likely_detach"
        assert report.adhesion.brim_width_mm >= 8
        brim_lines = [line for line in _all_advice(report) if _speaks_of_brim(line)]
        assert len(brim_lines) == 1, brim_lines

    def test_a_warp_prone_material_without_an_enclosure_gets_a_brim(self, tmp_path, free_tier):
        """A small ABS block: full contact, low warping risk, steady force
        balance — nothing else asks for a brim, but ABS on an open frame
        (or a printer nobody named) still wants one, and an enclosure does
        not."""
        from kiln.printability import analyze_printability

        block = _box_stl(tmp_path / "block.stl", 20.0, 20.0, 10.0)
        open_frame = analyze_printability(block, material="abs", printer_id="bambu_a1")
        unnamed = analyze_printability(block, material="abs")
        enclosed = analyze_printability(block, material="abs", printer_id="bambu_x1c")

        assert open_frame.warping.risk_level == "low"  # the premise
        assert open_frame.adhesion_force.risk_level == "secure"
        assert (open_frame.adhesion.brim_width_mm, unnamed.adhesion.brim_width_mm) == (5, 5)
        assert "without an enclosure" in open_frame.adhesion.rationale
        assert enclosed.adhesion.brim_width_mm == 0

    def test_the_decision_sees_an_enclosed_printer(self, tmp_path, free_tier):
        """Low contact wants a wider brim on an open frame than in an
        enclosure; the enclosure was never seen, because the doors read the
        printer profile as a dict and it is an object."""
        from kiln.printability import analyze_printability
        from kiln.printer_intelligence import get_printer_intel

        assert get_printer_intel("bambu_x1c").has_enclosure  # the premise
        stilts = _stilts_stl(tmp_path / "stilts.stl")
        enclosed = analyze_printability(stilts, material="pla", printer_id="bambu_x1c")
        open_frame = analyze_printability(stilts, material="pla")

        assert 2.0 <= enclosed.bed_adhesion.contact_percentage < 5.0
        assert (enclosed.adhesion.brim_width_mm, open_frame.adhesion.brim_width_mm) == (5, 8)

    def test_slice_and_estimate_speaks_with_the_report_decision(self, enclosure, tmp_path, free_tier, no_auth):
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin

        tool = _register(_EstimateToolsPlugin)["slice_and_estimate"]
        with patch("kiln.slicer.slice_file", _fake_slice(tmp_path, grams=29.93)):
            resp = tool(input_path=enclosure, printer_id="bambu_a1", material="PETG")

        assert resp["success"], resp
        assert resp["adhesion"] == resp["printability"]["adhesion"]
        assert resp["adhesion"]["brim_width_mm"] == 0
        assert resp["adhesion"]["rationale"].rstrip(".") in resp["message"]
        assert ".." not in resp["message"], resp["message"]
        warping = resp["printability"]["warping"]["recommendations"]
        assert not [line for line in warping if _speaks_of_brim(line)], warping


# ---------------------------------------------------------------------------
# 3. Every thin feature, where it is
# ---------------------------------------------------------------------------


def _near(region: dict[str, float], xy: tuple[float, float], tol: float) -> bool:
    return abs(region["x"] - xy[0]) <= tol and abs(region["y"] - xy[1]) <= tol


class TestEveryThinFeature:
    def test_the_material_floor_lists_every_feature_under_it(self, enclosure, monkeypatch):
        from kiln.printability import analyze_printability

        calls: list[dict] = []
        # Any floor above the thickest feature (1.5 mm) will do; this one is
        # the stand-in's, not a material's.
        _install_pro_wall_floor(monkeypatch, STAND_IN_FLOOR_MM, calls)
        walls = analyze_printability(enclosure, material="petg", printer_id="bambu_a1").thin_walls

        assert calls and calls[0]["material"] == "petg"
        assert walls.threshold_mm == pytest.approx(STAND_IN_FLOOR_MM)
        assert walls.threshold_basis == "material"
        assert walls.thin_wall_count > 0
        found = sorted({round(r["thickness_mm"], 1) for r in walls.problematic_regions})
        assert found == list(ENCLOSURE_WALL_FEATURES_MM), walls.problematic_regions
        standoffs = [r for r in walls.problematic_regions if round(r["thickness_mm"], 1) == 1.4]
        bosses = [r for r in walls.problematic_regions if round(r["thickness_mm"], 1) == 1.5]
        for xy in STANDOFF_XY:
            assert any(_near(r, xy, 3.5) for r in standoffs), (xy, standoffs)
        for xy in BOSS_XY:
            assert any(_near(r, xy, 4.5) for r in bosses), (xy, bosses)

    def test_a_wall_exactly_at_the_floor_is_not_thin(self, enclosure, monkeypatch):
        """The ledge is a CAD 1.2 mm wall that measures 1.1999 mm through
        float32 STL coordinates.  Against a 1.2 mm floor it is reported as
        1.2 mm, so it must not count as thin — the material rule, which reads
        the reported number, does not count it either."""
        from kiln.printability import analyze_printability

        _install_pro_wall_floor(monkeypatch, ENCLOSURE_WALL_FEATURES_MM[0])
        walls = analyze_printability(enclosure, material="petg").thin_walls

        assert walls.min_wall_thickness_mm == pytest.approx(ENCLOSURE_WALL_FEATURES_MM[0])
        assert walls.threshold_basis == "material"
        assert (walls.thin_wall_count, walls.problematic_regions) == (0, [])

    def test_the_score_still_deducts_only_for_walls_under_the_nozzle(self, enclosure, monkeypatch):
        from kiln.printability import analyze_printability

        _install_pro_wall_floor(monkeypatch, None)
        at_nozzle = analyze_printability(enclosure, material="petg")
        _install_pro_wall_floor(monkeypatch, STAND_IN_FLOOR_MM)
        at_floor = analyze_printability(enclosure, material="petg")

        assert at_nozzle.thin_walls.threshold_basis == "nozzle"
        assert at_nozzle.thin_walls.thin_wall_count == 0
        assert at_floor.thin_walls.thin_wall_count > 0
        assert at_floor.score == at_nozzle.score

    def test_without_a_material_floor_every_sub_nozzle_feature_is_listed(self, tmp_path, free_tier):
        from kiln.printability import analyze_printability

        fins = _fins_stl(tmp_path / "fins.stl", ((-15.0, 0.25), (15.0, 0.35)))
        walls = analyze_printability(fins).thin_walls

        assert walls.threshold_basis == "nozzle"
        assert walls.threshold_mm == pytest.approx(0.4)
        left = [r for r in walls.problematic_regions if abs(r["x"] + 15.0) < 2.0]
        right = [r for r in walls.problematic_regions if abs(r["x"] - 15.0) < 2.0]
        assert left and right, walls.problematic_regions
        assert min(r["thickness_mm"] for r in left) == pytest.approx(0.25, abs=0.02)
        assert min(r["thickness_mm"] for r in right) == pytest.approx(0.35, abs=0.02)


# ---------------------------------------------------------------------------
# 4. Two weights, each saying what it is
# ---------------------------------------------------------------------------


class TestWeightsSayWhichIsWhich:
    def test_the_cost_blocks_weight_says_it_is_a_mesh_estimate(self, enclosure, free_tier):
        from kiln.printability import analyze_printability

        cost = analyze_printability(enclosure, material="petg").cost

        assert cost is not None
        assert cost.filament_source == "mesh"
        assert cost.assumptions["infill_percent"] == pytest.approx(20.0)

    def test_slice_and_estimate_labels_both_weights(self, enclosure, tmp_path, free_tier, no_auth):
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin

        tool = _register(_EstimateToolsPlugin)["slice_and_estimate"]
        with patch("kiln.slicer.slice_file", _fake_slice(tmp_path, grams=29.93)):
            resp = tool(input_path=enclosure, printer_id="bambu_a1", material="PETG")

        assert resp["estimate"]["filament_used_grams"] == pytest.approx(29.93)
        assert resp["estimate"]["filament_source"] == "slicer_header"
        assert resp["printability"]["cost"]["filament_source"] == "mesh"


# ---------------------------------------------------------------------------
# Small meshes and a stand-in slicer
# ---------------------------------------------------------------------------


def _box_stl(path: Path, x: float, y: float, z: float) -> str:
    from trimesh.creation import box

    mesh = box(extents=(x, y, z))
    mesh.apply_translation((0, 0, z / 2.0))
    mesh.export(str(path))
    return str(path)


def _stilts_stl(path: Path) -> str:
    """A 40 x 40 x 3 mm plate on four 4 x 4 x 5 mm legs: 4% bed contact."""
    from trimesh.creation import box

    body = box(extents=(40.0, 40.0, 3.0))
    body.apply_translation((0, 0, 5.0 + 1.5))
    for x in (-16.0, 16.0):
        for y in (-16.0, 16.0):
            leg = box(extents=(4.0, 4.0, 5.1))
            leg.apply_translation((x, y, 5.1 / 2.0))
            body = body.union(leg)
    body.export(str(path))
    return str(path)


def _fins_stl(path: Path, fins: tuple[tuple[float, float], ...]) -> str:
    """A 60 x 40 x 3 mm plate with thin upright fins at ``(x, thickness)``."""
    from trimesh.creation import box

    body = box(extents=(60.0, 40.0, 3.0))
    body.apply_translation((0, 0, 1.5))
    for x, thickness in fins:
        fin = box(extents=(thickness, 20.0, 10.1))
        fin.apply_translation((x, 0, 2.95 + 10.1 / 2.0))
        body = body.union(fin)
    body.export(str(path))
    return str(path)


def _fake_slice(tmp_path: Path, *, grams: float) -> MagicMock:
    from kiln.slicer import SliceResult
    from kiln.slicer_filament import resolve_slice_filament

    gcode = tmp_path / "out.gcode"
    gcode.write_text(f";LAYER_CHANGE\nG1 X1 E1\n; filament used [g] = {grams}\n")
    result = SliceResult(
        success=True, output_path=str(gcode), slicer="prusa-slicer",
        message="Sliced", filament=resolve_slice_filament("PETG"),
    )
    return MagicMock(return_value=result)


# ---------------------------------------------------------------------------
# 5. The readiness check takes its supports verdict from the analyzer
# ---------------------------------------------------------------------------


def _cantilever_stl(path: Path) -> str:
    """A 10 x 10 x 20 mm pillar with a 30 mm arm sticking out one side."""
    from trimesh.creation import box

    pillar = box(extents=(10.0, 10.0, 20.0))
    pillar.apply_translation((0, 0, 10.0))
    arm = box(extents=(30.0, 10.0, 3.0))
    arm.apply_translation((5.0 + 15.0 - 0.01, 0, 20.0 - 1.5))
    body = pillar.union(arm)
    body.export(str(path))
    return str(path)


class TestReadinessSupports:
    def test_short_ceilings_do_not_make_a_part_need_supports(self, enclosure, free_tier):
        from kiln.generation.validation import analyze_mesh, can_print_now
        from kiln.printability import analyze_printability

        report = analyze_printability(enclosure, material="petg")
        assert analyze_mesh(enclosure).max_overhang_angle_deg > 60  # the old trigger
        assert report.overhangs.needs_supports is False

        result = can_print_now(enclosure, material="petg")

        assert result["verdict"] == "ready_to_print", result["issues"]
        assert (result["printability_score"], result["printability_grade"]) == (report.score, report.grade)

    def test_a_real_cantilever_still_needs_supports(self, tmp_path, free_tier):
        from kiln.generation.validation import can_print_now
        from kiln.printability import analyze_printability

        arm = _cantilever_stl(tmp_path / "arm.stl")
        assert analyze_printability(arm).overhangs.needs_supports is True  # the premise

        result = can_print_now(arm)

        assert result["verdict"] == "printable_with_supports", result["issues"]
        assert [i["type"] for i in result["issues"]] == ["needs_supports"]


class TestValidateAndPrepareScores:
    """validate_and_prepare's ``printability_score`` is analyze_printability's.

    It used to be the tally of the pipeline's own checks, which read 100 for
    the enclosure beside the score analyze_printability gives the same part.
    The tally is still there, as ``readiness_score``.
    """

    def test_the_score_is_the_analyzers_for_the_material_and_printer(self, enclosure, free_tier, no_auth):
        from kiln.plugins.validation_pipeline_tools import _ValidationPipelinePlugin
        from kiln.printability import analyze_printability

        validate = _register(_ValidationPipelinePlugin)["validate_and_prepare"]
        report = analyze_printability(enclosure, material="petg", printer_id="bambu_a1")

        result = validate(input_path=enclosure, printer_id="bambu_a1", material="petg")

        assert (result["printability_score"], result["printability_grade"]) == (report.score, report.grade)
        assert isinstance(result["readiness_score"], int) and result["score_breakdown"] is not None
        assert f"printability {report.score}/100" in result["summary"], result["summary"]

    def test_the_tally_keeps_its_own_name(self):
        from kiln.plugins._validation_pipeline_internals import score_phrase

        assert score_phrase({"printability_score": 89, "readiness_score": 100}) == "printability 89/100"
        assert score_phrase({"printability_score": None, "readiness_score": 0}) == "readiness 0/100"
