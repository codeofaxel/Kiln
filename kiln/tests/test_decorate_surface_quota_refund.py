"""decorate_surface failure exits give back the decoration they consumed.

The quota is check-and-increment at the top of the tool, so before this fix
every mid-pipeline failure (compile_failed, invalid content, wall-path
validation) silently burned a free-tier slot for a carve that was never
delivered.  These tests pin the contract: consume → fail → refund, and
consume → succeed → keep.  The pipeline itself is stubbed at the module
seams decorate_surface imports from at call time, so no OpenSCAD or
kiln-pro is needed.
"""

from __future__ import annotations

import struct

import pytest

from kiln import decoration_quota
from kiln.decoration_quota import DecorationQuota
from kiln.server import decorate_surface

_decorate = getattr(decorate_surface, "fn", decorate_surface)


class _SpyQuota(DecorationQuota):
    """Free-tier tracker that counts refund() calls."""

    def __init__(self, path):
        super().__init__(quota_path=path)
        self.refunds = 0
        self._get_tier = lambda: "free"  # noqa: SLF001 — test seam

    def refund(self) -> None:
        self.refunds += 1
        super().refund()


@pytest.fixture()
def spy_quota(tmp_path, monkeypatch):
    """Install a spying free-tier tracker as the module singleton."""
    q = _SpyQuota(tmp_path / "decoration_usage.json")
    monkeypatch.setattr(decoration_quota, "_quota", q)
    return q


@pytest.fixture()
def dummy_stl(tmp_path):
    """A minimal one-triangle binary STL — enough to pass file validation."""
    p = tmp_path / "body.stl"
    with open(p, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", 1))
        fh.write(struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0))
    return str(p)


def _stub_pipeline_until_compile(monkeypatch, tmp_path, *, compile_result):
    """Stub face finding, text prep, and SCAD generation; control compile."""
    import kiln.emboss_generator as emboss
    import kiln.image_to_surface as i2s
    import kiln.surface_intelligence as surf

    face_info = {
        "face_name": "top",
        "width_mm": 80.0,
        "height_mm": 80.0,
        "area_mm2": 6400.0,
        "center": (0.0, 0.0, 10.0),
    }
    # The decorate door resolves faces through the shared resolver now;
    # faking the resolver fakes both of its arms at once.
    monkeypatch.setattr(
        surf, "resolve_decoratable_face", lambda _p, _f=None: face_info
    )
    monkeypatch.setattr(
        emboss, "measure_text_block_mm", lambda *_a, **_k: (40.0, 12.0, 0.0, 0.0)
    )
    monkeypatch.setattr(
        i2s, "generate_text_image", lambda *_a, **_k: {"type": "text"}
    )
    scad = tmp_path / "emboss.scad"
    scad.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(
        emboss,
        "generate_emboss_scad",
        lambda **_k: {
            "scad_path": str(scad),
            "output_stl_path": str(tmp_path / "out.stl"),
        },
    )
    monkeypatch.setattr(
        emboss, "compile_embossed_model", lambda *_a, **_k: compile_result
    )
    return face_info


def test_compile_failed_exit_refunds_the_consumed_decoration(
    spy_quota, dummy_stl, tmp_path, monkeypatch
) -> None:
    _stub_pipeline_until_compile(
        monkeypatch,
        tmp_path,
        compile_result={"success": False, "error": "synthetic failure"},
    )
    result = _decorate(model_path=dummy_stl, content="text:HI")
    assert result.get("status") == "compile_failed"
    assert spy_quota.refunds == 1
    assert spy_quota.get_status().used == 0  # slot handed back


def test_validation_failure_after_the_check_refunds(spy_quota, dummy_stl) -> None:
    # face='wall' rejects non-text content — a failure exit past the
    # quota check that must not keep the slot.
    result = _decorate(
        model_path=dummy_stl,
        content="/nonexistent/logo.png",
        content_type="image",
        face="wall",
    )
    assert result.get("success") is False
    assert spy_quota.refunds == 1
    assert spy_quota.get_status().used == 0


def test_quota_exceeded_exit_does_not_refund(spy_quota, dummy_stl) -> None:
    # An over-cap call never consumed anything — refunding here would
    # hand back a slot that a *previous, delivered* decoration used.
    for _ in range(3):
        ok, _err = spy_quota.check_and_increment()
        assert ok is True
    result = _decorate(model_path=dummy_stl, content="text:HI")
    assert result.get("code") == "DECORATION_QUOTA_EXCEEDED", result
    assert spy_quota.refunds == 0
    assert spy_quota.get_status().used == 3


def test_successful_decoration_keeps_the_consumed_slot(
    spy_quota, dummy_stl, tmp_path, monkeypatch
) -> None:
    out_stl = tmp_path / "out.stl"
    out_stl.write_bytes(b"\0" * 200)
    _stub_pipeline_until_compile(
        monkeypatch,
        tmp_path,
        compile_result={
            "success": True,
            "stl_path": str(out_stl),
            "file_size": 200,
            "compile_time_seconds": 0.1,
        },
    )
    import kiln.emboss_generator as emboss
    import kiln.server as server

    monkeypatch.setattr(emboss, "check_boolean_success", lambda *_a: True)
    # Keep the success tail hermetic (no inspect bundle / managed assets).
    monkeypatch.setattr(
        server, "_finish_decoration_result", lambda result_dict, *, content: result_dict
    )
    result = _decorate(model_path=dummy_stl, content="text:HI")
    assert result.get("success") is True, result
    assert spy_quota.refunds == 0
    assert spy_quota.get_status().used == 1
