"""Tests for kiln.fusion_check — the shared unfused-attachment detector.

Regression anchor (2026-08-29): a handle composed flush against a curved
wall touches it along a line, shares no volume, and passes every
watertight/manifold check — the composed "one part" is two disconnected
shells.  These tests build that shape (tangent contact, sub-millimeter
gap) and prove the detector flags it and the composition doors surface
it, while genuinely separate multi-part output stays unflagged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.fusion_check import (  # noqa: E402
    EMBEDMENT_FLOOR_MM,
    attach_fusion_report,
    check_fusion,
    embedment_floor_mm,
)

# ---------------------------------------------------------------------------
# Fixture geometry: box host + cylinder "attachment" at a chosen gap
# ---------------------------------------------------------------------------

def _box_and_cylinder_stl(path, *, gap_mm: float):
    """Box (faces at x=+-5) + vertical cylinder r=3 tangent to the x=+5 face.

    ``gap_mm=0`` is exact tangency: the cylinder's surface touches the
    face along a vertical line (a cylinder section vertex lies exactly
    on it), and at the cylinder's flanks the air channel opens up —
    the mug-handle failure shape in miniature.  Positive values slide
    the cylinder away by that much.
    """
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    cyl = trimesh.creation.cylinder(radius=3.0, height=6.0, sections=64)
    # Seated on the box's own floor (z=-5), so the two parts share a bed:
    # the separated case below is then a plate of parts, not a piece in
    # the air, and only the GAP is under test.
    cyl.apply_translation((8.0 + gap_mm, 0.0, -2.0))
    combined = trimesh.util.concatenate([box, cyl])
    combined.export(path)
    return path


def _stand_with_floating_cells_stl(path, *, lift_mm: float = 6.0, cells: int = 3):
    """A base on the bed plus hexagonal cells hanging in the air above it.

    The 2026-10-19 honeycomb phone stand in miniature: an agent's OpenSCAD
    placed every honeycomb cell on a tilted plane that never met the base,
    so 61 prisms floated 6-28 mm up and the compile said nothing.  Nothing
    joins a cell to the base, no cell sits on the bed.
    """
    base = trimesh.creation.box(extents=(40.0, 30.0, 4.0))
    base.apply_translation((0.0, 0.0, 2.0))  # z 0..4, on the bed
    parts = [base]
    for i in range(cells):
        cell = trimesh.creation.cylinder(radius=3.0, height=3.0, sections=6)
        cell.apply_translation((-12.0 + 12.0 * i, 0.0, lift_mm + 1.5))
        parts.append(cell)
    trimesh.util.concatenate(parts).export(path)
    return path


def _plate_of_parts_stl(path):
    """Two separate parts, both sitting on the bed: a deliberate plate."""
    a = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    a.apply_translation((0.0, 0.0, 5.0))
    b = trimesh.creation.box(extents=(8.0, 8.0, 4.0))
    b.apply_translation((25.0, 0.0, 2.0))
    trimesh.util.concatenate([a, b]).export(path)
    return path


# ---------------------------------------------------------------------------
# check_fusion
# ---------------------------------------------------------------------------

class TestCheckFusion:
    def test_single_body_is_fused(self, tmp_path) -> None:
        p = tmp_path / "one.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(p)
        report = check_fusion(str(p))
        assert report["checked"] is True
        assert report["fused"] is True
        assert report["body_count"] == 1
        assert report["findings"] == []

    def test_tangent_contact_is_flagged_touching(self, tmp_path) -> None:
        p = _box_and_cylinder_stl(tmp_path / "tangent.stl", gap_mm=0.0)
        report = check_fusion(str(p))
        assert report["fused"] is False
        assert report["body_count"] == 2
        assert len(report["findings"]) == 1
        finding = report["findings"][0]
        assert finding["relation"] == "touching"
        assert finding["gap_mm"] <= 0.05
        assert "share" in finding["message"]

    def test_submillimeter_gap_is_flagged_near(self, tmp_path) -> None:
        p = _box_and_cylinder_stl(tmp_path / "gap.stl", gap_mm=0.3)
        report = check_fusion(str(p))
        assert report["fused"] is False
        finding = report["findings"][0]
        assert finding["relation"] == "near"
        assert finding["gap_mm"] == pytest.approx(0.3, abs=0.05)

    def test_separate_parts_pass_when_multibody_is_allowed(self, tmp_path) -> None:
        p = _box_and_cylinder_stl(tmp_path / "apart.stl", gap_mm=12.0)
        report = check_fusion(str(p), expect_single_body=None)
        assert report["fused"] is False
        assert report["findings"] == []

    def test_separate_parts_flagged_when_one_body_expected(self, tmp_path) -> None:
        p = _box_and_cylinder_stl(tmp_path / "apart.stl", gap_mm=12.0)
        report = check_fusion(str(p), expect_single_body=True)
        assert report["findings"], "expected a separated-bodies finding"
        assert report["findings"][0]["relation"] == "separated"

    def test_unloadable_mesh_is_honest_not_a_pass(self, tmp_path) -> None:
        p = tmp_path / "junk.stl"
        p.write_bytes(b"not an stl")
        report = check_fusion(str(p))
        assert report["checked"] is False
        assert "reason" in report


# ---------------------------------------------------------------------------
# attach_fusion_report
# ---------------------------------------------------------------------------

class TestAttachFusionReport:
    def test_annotates_touching_pair(self, tmp_path) -> None:
        p = _box_and_cylinder_stl(tmp_path / "tangent.stl", gap_mm=0.0)
        response = attach_fusion_report({"success": True}, str(p))
        assert response["fusion"]["fused"] is False
        assert any("unfused geometry" in w for w in response["warnings"])

    def test_clean_single_body_left_untouched(self, tmp_path) -> None:
        p = tmp_path / "one.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(p)
        response = attach_fusion_report({"success": True}, str(p))
        assert "fusion" not in response
        assert "warnings" not in response

    def test_never_raises_on_missing_path(self) -> None:
        response = attach_fusion_report({"success": True}, "/nope/missing.stl")
        assert response == {"success": True}
        assert attach_fusion_report({"success": True}, None) == {"success": True}


# ---------------------------------------------------------------------------
# embedment floor
# ---------------------------------------------------------------------------

class TestEmbedmentFloor:
    def test_absolute_floor_holds_for_thin_contacts(self) -> None:
        assert embedment_floor_mm(0.0) == EMBEDMENT_FLOOR_MM
        assert embedment_floor_mm(1.0) == EMBEDMENT_FLOOR_MM

    def test_scales_with_contact_radius(self) -> None:
        assert embedment_floor_mm(5.0) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Door wiring: compose_models surfaces the finding (the 2026-08-29 A/B)
# ---------------------------------------------------------------------------

class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


@pytest.fixture
def mesh_tools():
    from kiln.plugins.mesh_tools import plugin

    fake = _FakeMCP()
    plugin.register(fake)
    return fake.tools


def _identity_bundle(response, **_kwargs):
    return response


class TestComposeDoorSurfacesFusion:
    @patch("kiln.server._check_auth", return_value=None)
    def test_compose_models_flags_tangent_pair(
        self, _auth, mesh_tools, tmp_path
    ) -> None:
        host = tmp_path / "host.stl"
        part = tmp_path / "part.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(host)
        cyl = trimesh.creation.cylinder(radius=3.0, height=6.0, sections=64)
        cyl.apply_translation((8.0, 0.0, 0.0))
        cyl.export(part)
        out = tmp_path / "composed.stl"

        try:
            bundle_patch = patch(
                "kiln_pro.plugins.git_render_tools.attach_inspect_bundle",
                side_effect=_identity_bundle,
            )
            bundle_patch.start()
        except (ImportError, ModuleNotFoundError):
            bundle_patch = None
        try:
            result = mesh_tools["compose_models"](
                file_paths=[str(host), str(part)], output_path=str(out),
            )
        finally:
            if bundle_patch is not None:
                bundle_patch.stop()

        assert result["success"] is True
        assert result["fusion"]["fused"] is False
        assert result["fusion"]["findings"][0]["relation"] == "touching"
        assert any("unfused geometry" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# diagnose_mesh: a detached ATTACHMENT is not debris (the misdiagnosis fix)
# ---------------------------------------------------------------------------

class TestDiagnoseMeshDetachedAttachment:
    def test_substantial_touching_component_reads_as_unfused_attachment(
        self, tmp_path
    ) -> None:
        from kiln.mesh_diagnostics import diagnose_mesh

        p = _box_and_cylinder_stl(tmp_path / "tangent.stl", gap_mm=0.0)
        report = diagnose_mesh(str(p))
        assert report.detached_attachment is True
        assert any("never fused" in d for d in report.defects)
        recs = " ".join(report.recommendations)
        assert "Fuse the detached attachment" in recs
        # The old advice — delete everything but the largest component —
        # must not be the headline for a substantial near-touching body.
        assert "Keep only the largest component" not in recs
        # An object that prints in pieces is never a cosmetic note.
        assert report.severity in ("moderate", "severe")

    def test_tiny_far_fragment_still_reads_as_debris(self, tmp_path) -> None:
        from kiln.mesh_diagnostics import diagnose_mesh

        box = trimesh.creation.box(extents=(10, 10, 10))
        sliver = trimesh.creation.box(extents=(0.5, 0.5, 0.5))
        sliver.apply_translation((30.0, 0.0, 0.0))
        p = tmp_path / "debris.stl"
        trimesh.util.concatenate([box, sliver]).export(p)
        report = diagnose_mesh(str(p))
        assert report.detached_attachment is False
        assert any("Remove floating fragments" in r for r in report.recommendations)


# ---------------------------------------------------------------------------
# No-scipy fallback: the check must not silently no-op on a clean install
# ---------------------------------------------------------------------------

class TestNoScipyFallback:
    def test_tangent_still_flagged_without_scipy(self, tmp_path, monkeypatch) -> None:
        import sys

        p = _box_and_cylinder_stl(tmp_path / "tangent.stl", gap_mm=0.0)
        # Blocking the module makes `from scipy.spatial import cKDTree`
        # raise ImportError, forcing the pure-numpy shortlist path.
        monkeypatch.setitem(sys.modules, "scipy", None)
        monkeypatch.setitem(sys.modules, "scipy.spatial", None)
        report = check_fusion(str(p))
        assert report["fused"] is False
        assert report["findings"][0]["relation"] == "touching"
        assert "gap_unmeasured" not in report


# ---------------------------------------------------------------------------
# compile_scad door: agent-authored SCAD is where the incident came from
# ---------------------------------------------------------------------------

@pytest.fixture
def design_tools():
    from kiln.plugins.design_tools import plugin

    fake = _FakeMCP()
    plugin.register(fake)
    return fake.tools


class TestCompileScadDoorSurfacesFusion:
    def test_compile_scad_flags_tangent_result(
        self, design_tools, tmp_path
    ) -> None:
        stl = _box_and_cylinder_stl(tmp_path / "tangent.stl", gap_mm=0.0)

        try:
            bundle_patch = patch(
                "kiln_pro.plugins.git_render_tools.attach_inspect_bundle",
                side_effect=_identity_bundle,
            )
            bundle_patch.start()
        except (ImportError, ModuleNotFoundError):
            bundle_patch = None
        try:
            with patch(
                "kiln.parametric.compile_scad_code", return_value=str(stl)
            ):
                result = design_tools["compile_scad"](scad_code="cube(1);")
        finally:
            if bundle_patch is not None:
                bundle_patch.stop()

        assert result["success"] is True
        assert result["fusion"]["findings"][0]["relation"] == "touching"
        assert any("unfused geometry" in w for w in result["warnings"])


class TestPrintInPlaceIsNotMisdiagnosed:
    """A designed clearance must never be told to fuse itself shut."""

    def test_hinge_clearance_is_not_called_a_detached_attachment(
        self, tmp_path
    ) -> None:
        from kiln.mesh_diagnostics import diagnose_mesh

        # Two substantial bodies 0.35mm apart — a print-in-place hinge
        # clearance, well inside the fusion check's "near" band.
        p = _box_and_cylinder_stl(tmp_path / "hinge.stl", gap_mm=0.35)
        report = diagnose_mesh(str(p))
        assert report.detached_attachment is False
        recs = " ".join(report.recommendations)
        assert "Fuse the detached attachment" not in recs

    def test_compose_door_still_mentions_it_with_a_caveat(
        self, tmp_path
    ) -> None:
        # The compose side DOES surface a near gap, because there the
        # caller asked to build one part — but says a designed clearance
        # needs no action, rather than ordering a fix.
        p = _box_and_cylinder_stl(tmp_path / "near.stl", gap_mm=0.35)
        response = attach_fusion_report({"success": True}, str(p))
        message = response["fusion"]["findings"][0]["message"]
        assert "designed clearance" in message


# ---------------------------------------------------------------------------
# Pieces in the air: void, captive or floating (2026-10-19)
# ---------------------------------------------------------------------------

def _box(extents, at):
    b = trimesh.creation.box(extents=extents)
    b.apply_translation(at)
    return b


def _save(path, parts):
    trimesh.util.concatenate(parts).export(path)
    return path


class TestPiecesInTheAir:
    """A separated body used to be skipped as "maybe a plate of parts".  A
    plate sits on the bed; a body above it is a void (the inner shell of a
    hollow part), a captive piece (held inside the part) or a floating one
    (surrounded by air) -- and the geometry decides, there is no flag."""

    def test_floating_cells_are_one_finding_with_the_gap_to_the_part(self, tmp_path) -> None:
        p = _stand_with_floating_cells_stl(tmp_path / "stand.stl", lift_mm=6.0)
        report = check_fusion(str(p), expect_single_body=None)
        assert report["body_count"] == 4
        assert report["grounded_bodies"] == 1
        assert report["floating_bodies"] == 3
        assert report["captive_bodies"] == 0
        floating = [f for f in report["findings"] if f["relation"] == "floating"]
        assert len(floating) == 1, report["findings"]
        finding = floating[0]
        assert len(finding["bodies"]) == 3
        assert finding["lift_mm"]["min"] == pytest.approx(6.0, abs=0.1)
        # The base is 4 mm tall, so the cells hang 2 mm above its top: the
        # number the agent moves them by, not the height above the bed.
        assert finding["gap_mm"]["min"] == pytest.approx(2.0, abs=0.15)
        assert "2.0 mm away" in finding["message"]
        assert "float" in finding["message"] and "loose" in finding["message"]

    def test_parts_sitting_on_the_bed_are_a_plate_not_a_finding(self, tmp_path) -> None:
        p = _plate_of_parts_stl(tmp_path / "plate.stl")
        report = check_fusion(str(p), expect_single_body=None)
        assert report["body_count"] == 2
        assert report["floating_bodies"] == 0
        assert report["grounded_bodies"] == 2
        assert report["findings"] == []

    def test_a_piece_touching_its_host_is_said_once(self, tmp_path) -> None:
        # A tangent cylinder whose bottom is 2 mm above the host's floor:
        # the contact finding already names it, so it is not also "floating".
        box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        cyl = trimesh.creation.cylinder(radius=3.0, height=6.0, sections=64)
        cyl.apply_translation((8.0, 0.0, 0.0))
        p = tmp_path / "raised_tangent.stl"
        trimesh.util.concatenate([box, cyl]).export(p)
        report = check_fusion(str(p))
        assert [f["relation"] for f in report["findings"]] == ["touching"]
        assert report["floating_bodies"] == 0

    def test_lift_is_measured_from_the_lowest_substantial_body(self, tmp_path) -> None:
        # Exported at z=50: the bed is the part's own floor, not z=0.
        p = _save(tmp_path / "raised.stl", [_box((20, 20, 4), (0, 0, 52)), _box((4, 4, 4), (0, 0, 62))])
        report = check_fusion(str(p))
        assert report["grounded_bodies"] == 1
        assert report["floating_bodies"] == 1
        floating = [f for f in report["findings"] if f["relation"] == "floating"][0]
        assert floating["lift_mm"]["min"] == pytest.approx(10.0, abs=0.1)

    def test_a_sliver_at_the_floor_does_not_float_the_whole_model(self, tmp_path) -> None:
        # One stray 0.4 mm fragment at z=0 under a part that starts at z=2:
        # the floor is the lowest SUBSTANTIAL body, so nothing floats.
        p = _save(
            tmp_path / "debris.stl",
            [_box((0.4, 0.4, 0.2), (30, 0, 0.1)), _box((20, 20, 10), (0, 0, 7))],
        )
        report = check_fusion(str(p))
        assert report["floating_bodies"] == 0
        assert report["findings"] == []

    def test_a_hollow_parts_inner_shell_is_a_void_not_a_piece(self, tmp_path) -> None:
        inner = _box((14, 14, 14), (0, 0, 10))
        inner.invert()
        p = _save(tmp_path / "hollow.stl", [_box((20, 20, 20), (0, 0, 10)), inner])
        report = check_fusion(str(p))
        assert report["body_count"] == 2
        assert report["void_shells"] == 1
        assert report["floating_bodies"] == 0
        assert report["findings"] == []

    def test_a_ball_inside_a_hollow_part_is_captive_and_warned_not_refused(self, tmp_path) -> None:
        inner = _box((14, 14, 14), (0, 0, 10))
        inner.invert()
        ball = trimesh.creation.icosphere(subdivisions=2, radius=3.0)
        ball.apply_translation((0, 0, 10))
        p = _save(tmp_path / "rattle.stl", [_box((20, 20, 20), (0, 0, 10)), inner, ball])
        report = check_fusion(str(p))
        assert report["void_shells"] == 1
        assert report["captive_bodies"] == 1
        assert report["floating_bodies"] == 0
        assert [f["relation"] for f in report["findings"]] == ["captive"]
        assert "supports inside" in report["findings"][0]["message"]

    def test_a_print_in_place_pin_in_its_knuckles_is_a_clearance_not_a_float(self, tmp_path) -> None:
        import numpy as np

        turn = trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0])
        k1 = trimesh.creation.annulus(r_min=2.5, r_max=5.0, height=8.0)
        k1.apply_transform(turn)
        k1.apply_translation((4, 0, 5))
        k2 = k1.copy()
        k2.apply_translation((12, 0, 0))
        pin = trimesh.creation.cylinder(radius=2.0, height=24.0, sections=32)
        pin.apply_transform(turn)
        pin.apply_translation((8, 0, 5))
        p = _save(tmp_path / "hinge.stl", [k1, k2, pin])
        report = check_fusion(str(p))
        assert report["floating_bodies"] == 0
        assert {f["relation"] for f in report["findings"]} == {"near"}

    def test_a_lintel_resting_on_two_posts_touches_but_one_lifted_a_millimetre_floats(self, tmp_path) -> None:
        posts = [_box((4, 4, 20), (-10, 0, 10)), _box((4, 4, 20), (10, 0, 10))]
        resting = _save(tmp_path / "lintel.stl", posts + [_box((26, 4, 3), (0, 0, 21.5))])
        lifted = _save(tmp_path / "lintel_gap.stl", posts + [_box((26, 4, 3), (0, 0, 22.5))])
        assert check_fusion(str(resting))["floating_bodies"] == 0
        report = check_fusion(str(lifted))
        assert report["floating_bodies"] == 1
        gap = [f for f in report["findings"] if f["relation"] == "floating"][0]["gap_mm"]
        assert gap["min"] == pytest.approx(1.0, abs=0.1)

    def test_a_tall_front_lip_does_not_hide_scrap_behind_it(self, tmp_path) -> None:
        # The envelope trap: a 30 mm lip makes the part's convex hull a wedge
        # that contains cells hanging 2 mm over the base behind it.  Covered
        # from below but open to the sky, they float.
        cells = [_box((6, 6, 3), (x, 20, 7.5)) for x in range(-15, 16, 10)]
        p = _save(
            tmp_path / "lip.stl",
            [_box((40, 40, 4), (0, 0, 2)), _box((40, 4, 30), (0, -18, 15)), *cells],
        )
        report = check_fusion(str(p))
        assert report["floating_bodies"] == 4
        assert report["captive_bodies"] == 0


class TestAttachFusionReportRefusesFloating:
    def test_refusal_carries_the_code_the_fix_and_no_success(self, tmp_path) -> None:
        from kiln.fusion_check import FLOATING_REFUSAL_CODE

        p = _stand_with_floating_cells_stl(tmp_path / "stand.stl")
        response = attach_fusion_report(
            {"success": True, "message": "Compiled to stand.stl", "stl_path": str(p)},
            str(p),
            refuse_floating=True,
        )
        assert response["success"] is False
        assert response["code"] == FLOATING_REFUSAL_CODE
        assert response["error"].startswith("Not made: 3 pieces float")
        assert "2.0 mm away" in response["error"]
        assert response["message"] == response["error"]
        assert response["fusion"]["floating_bodies"] == 3
        assert any("floating pieces" in w for w in response["warnings"])
        # The file stays so the agent can look at what it wrote.
        assert response["stl_path"] == str(p)

    def test_without_the_flag_it_is_a_loud_warning(self, tmp_path) -> None:
        p = _stand_with_floating_cells_stl(tmp_path / "stand.stl")
        response = attach_fusion_report(
            {"success": True, "message": "Compiled to stand.stl"}, str(p)
        )
        assert response["success"] is True
        assert response["message"].startswith("Compiled to stand.stl")
        assert "float" in response["message"]

    def test_a_captive_piece_is_a_warning_even_with_the_flag(self, tmp_path) -> None:
        inner = _box((14, 14, 14), (0, 0, 10))
        inner.invert()
        ball = trimesh.creation.icosphere(subdivisions=2, radius=3.0)
        ball.apply_translation((0, 0, 10))
        p = _save(tmp_path / "rattle.stl", [_box((20, 20, 20), (0, 0, 10)), inner, ball])
        response = attach_fusion_report(
            {"success": True, "message": "Compiled"}, str(p), refuse_floating=True
        )
        assert response["success"] is True
        assert any("captive pieces" in w for w in response["warnings"])

    def test_plate_leaves_the_response_alone(self, tmp_path) -> None:
        p = _plate_of_parts_stl(tmp_path / "plate.stl")
        response = attach_fusion_report(
            {"success": True, "message": "Compiled to plate.stl"}, str(p), refuse_floating=True
        )
        assert response["success"] is True
        assert response["message"] == "Compiled to plate.stl"
        assert "warnings" not in response


class TestMakeDoorsRefuseFloating:
    """Every make door that compiles geometry refuses a piece in the air,
    through the one shared helper -- the compile the incident came through
    first -- and draws no preview of it."""

    def _bundle_patch(self):
        try:
            p = patch(
                "kiln_pro.plugins.git_render_tools.attach_inspect_bundle",
                side_effect=_identity_bundle,
            )
            p.start()
            return p
        except (ImportError, ModuleNotFoundError):
            return None

    @patch("kiln.server._check_auth", return_value=None)
    def test_compile_scad(self, _auth, design_tools, tmp_path) -> None:
        stl = _stand_with_floating_cells_stl(tmp_path / "stand.stl")
        bp = self._bundle_patch()
        try:
            with patch("kiln.parametric.compile_scad_code", return_value=str(stl)):
                result = design_tools["compile_scad"](scad_code="cube(1);")
        finally:
            if bp is not None:
                bp.stop()
        assert result["success"] is False
        assert result["code"] == "FLOATING_PIECES"
        assert result["fusion"]["floating_bodies"] == 3
        assert "float" in result["error"]
        assert "preview" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_compile_scad_passes_a_plate(self, _auth, design_tools, tmp_path) -> None:
        stl = _plate_of_parts_stl(tmp_path / "plate.stl")
        bp = self._bundle_patch()
        try:
            with patch("kiln.parametric.compile_scad_code", return_value=str(stl)):
                result = design_tools["compile_scad"](scad_code="cube(1);")
        finally:
            if bp is not None:
                bp.stop()
        assert result["success"] is True
        assert "fusion" not in result

    @patch("kiln.server._check_auth", return_value=None)
    def test_tweak_and_compile_scad(self, _auth, design_tools, tmp_path) -> None:
        stl = _stand_with_floating_cells_stl(tmp_path / "stand.stl")
        bp = self._bundle_patch()
        try:
            with patch(
                "kiln.parametric.tweak_and_compile",
                return_value={"updated_code": "cube(2);", "stl_path": str(stl), "warnings": []},
            ):
                result = design_tools["tweak_and_compile_scad"](
                    scad_code="cube(1);", parameter_name="size", new_value=2.0,
                )
        finally:
            if bp is not None:
                bp.stop()
        assert result["success"] is False
        assert result["code"] == "FLOATING_PIECES"
        assert result["fusion"]["floating_bodies"] == 3

    @patch("kiln.server._check_auth", return_value=None)
    def test_generate_from_template(self, _auth, tmp_path, monkeypatch) -> None:
        import os

        from kiln import server as _server
        from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus

        stl = _stand_with_floating_cells_stl(tmp_path / "stand.stl")

        class _Stub:
            name = "openscad"

            def generate(self, prompt, *, format="stl", **kwargs):
                return GenerationJob(
                    id="job-1", provider="openscad", prompt=prompt,
                    status=GenerationStatus.SUCCEEDED, progress=100,
                    created_at=0.0, format=format,
                )

            def download_result(self, job_id, output_dir=None):
                return GenerationResult(
                    job_id=job_id, provider="openscad", local_path=str(stl),
                    format="stl", file_size_bytes=os.path.getsize(stl), prompt="",
                )

        monkeypatch.setattr(_server, "_generation_providers", {"openscad": _Stub()})
        bp = self._bundle_patch()
        try:
            result = _server.generate_from_template(
                "phone_stand", {"phone_width": 80.0, "base_depth": 60.0, "thickness": 5.0},
            )
        finally:
            if bp is not None:
                bp.stop()
        assert result["success"] is False, result
        assert result["code"] == "FLOATING_PIECES"
        assert result["fusion"]["floating_bodies"] == 3

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.design_reasoning.design_to_gcode")
    def test_design_to_gcode_pipeline(self, mock_d2g, _auth, tmp_path) -> None:
        from kiln.design_reasoning import DesignToGCodeResult
        from kiln.server import design_to_gcode_pipeline

        stl = _stand_with_floating_cells_stl(tmp_path / "stand.stl")
        mock_d2g.return_value = DesignToGCodeResult(
            description="honeycomb stand", template_id="phone_stand",
            stl_file=str(stl), steps_completed=["template_search", "stl_rendering"],
            success=True,
        )
        result = design_to_gcode_pipeline("honeycomb stand")
        assert result["success"] is False
        assert result["code"] == "FLOATING_PIECES"
        assert result["fusion"]["floating_bodies"] == 3


# ---------------------------------------------------------------------------
# The colour a part is SHOWN in rides the compile doors
# ---------------------------------------------------------------------------

class TestDeclaredColor:
    def test_callers_word_wins_and_is_recorded_as_a_display_hint(self) -> None:
        from kiln.declared_color import SHOWN_NOTE, attach_declared_color

        response: dict = {}
        assert attach_declared_color(response, "teal", scad_code='color("red") cube(1);') == "teal"
        assert response["color"]["shown"] == "teal"
        assert response["color"]["source"] == "caller"
        assert response["color"]["note"] == SHOWN_NOTE
        # A name Kiln knows resolves to the hex the stage and the AMS use.
        assert response["color"]["hex"] == "#008080"

    def test_a_single_colour_in_the_source_is_read(self) -> None:
        from kiln.declared_color import attach_declared_color, single_scad_color

        assert single_scad_color('color("Teal") { cube(1); color("teal") sphere(1); }') == "teal"
        assert single_scad_color("color('#2aa198') cube(1);") == "#2aa198"
        response: dict = {}
        assert attach_declared_color(response, "", scad_code='color("orange") cube(1);') == "orange"
        assert response["color"]["source"] == "scad"

    def test_two_colours_or_a_vector_pick_nothing(self) -> None:
        from kiln.declared_color import attach_declared_color, single_scad_color

        assert single_scad_color('color("red") cube(1); color("blue") sphere(1);') is None
        assert single_scad_color("color([0.2, 0.6, 0.6]) cube(1);") is None
        assert single_scad_color("cube(1);") is None
        response: dict = {}
        assert attach_declared_color(response, "", scad_code="cube(1);") is None
        assert "color" not in response

    @patch("kiln.server._check_auth", return_value=None)
    def test_compile_scad_hands_the_colour_to_its_preview(self, _auth, design_tools, tmp_path) -> None:
        p = tmp_path / "cube.stl"
        trimesh.creation.box(extents=(10, 10, 10)).export(p)
        seen: dict = {}

        def _capture(response, **kwargs):
            seen.update(kwargs)
            return response

        try:
            bp = patch("kiln_pro.plugins.git_render_tools.attach_inspect_bundle", side_effect=_capture)
            bp.start()
        except (ImportError, ModuleNotFoundError):
            bp = None
        try:
            with patch("kiln.parametric.compile_scad_code", return_value=str(p)):
                result = design_tools["compile_scad"](scad_code="cube(10);", color="teal")
        finally:
            if bp is not None:
                bp.stop()
        assert result["success"] is True
        assert result["color"]["shown"] == "teal"
        if bp is not None:
            assert seen.get("body_color") == "teal"


class TestPastThePairwiseBudget:
    def test_twenty_raised_letters_on_a_sign_all_read_as_touching_and_none_float(self, tmp_path) -> None:
        # The pairwise pass stops at eight pairs; the ninth letter touches
        # the plate exactly as the first eight do and must not be refused
        # as floating for want of a measurement.
        letters = [_box((3, 6, 1), (x, 0, 2.5)) for x in range(-38, 40, 4)]
        p = _save(tmp_path / "sign.stl", [_box((80, 20, 2), (0, 0, 1)), *letters])
        report = check_fusion(str(p))
        assert report["body_count"] == 21
        assert report["floating_bodies"] == 0
        assert {f["relation"] for f in report["findings"]} == {"touching"}
        named = sorted(i for f in report["findings"] for i in f["bodies"])
        assert named == list(range(1, 22)) or set(named) >= set(range(2, 22))
        response = attach_fusion_report({"success": True}, str(p), refuse_floating=True)
        assert response["success"] is True
