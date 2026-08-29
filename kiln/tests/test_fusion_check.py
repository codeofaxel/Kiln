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
