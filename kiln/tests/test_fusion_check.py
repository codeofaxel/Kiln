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
    cyl.apply_translation((8.0 + gap_mm, 0.0, 0.0))
    combined = trimesh.util.concatenate([box, cyl])
    combined.export(path)
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
