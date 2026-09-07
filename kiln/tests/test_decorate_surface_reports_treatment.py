"""decorate_surface says what the engine DID with the image, not what was asked.

``image_style`` is a hint.  A bi-level mark routed through the trace door
is carved as a stencil whatever style the caller named, and a heightmap
carve reports the style it actually ran plus whether the image was
treated as a mark or as relief.  The pipeline is stubbed at the seams
decorate_surface imports at call time, past the point where the real
image classification and trace have already happened.
"""
from __future__ import annotations

import struct

import pytest

from kiln.server import decorate_surface

_decorate = getattr(decorate_surface, "fn", decorate_surface)


@pytest.fixture()
def dummy_stl(tmp_path):
    p = tmp_path / "body.stl"
    with open(p, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", 1))
        fh.write(struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0))
    return str(p)


@pytest.fixture()
def logo_png(tmp_path):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([15, 15, 105, 45], outline=(255, 255, 255, 255), width=6)
    d.rectangle([50, 60, 70, 105], fill=(255, 100, 40, 255))
    p = tmp_path / "logo.png"
    img.save(p)
    return str(p)


def _stub_after_content_prep(monkeypatch, tmp_path):
    import kiln.emboss_generator as emboss
    import kiln.surface_intelligence as surf

    face_info = {
        "face_name": "top", "width_mm": 80.0, "height_mm": 80.0,
        "area_mm2": 6400.0, "center": (0.0, 0.0, 10.0), "normal": (0.0, 0.0, 1.0),
    }
    monkeypatch.setattr(surf, "resolve_decoratable_face", lambda _p, _f=None: face_info)
    scad = tmp_path / "emboss.scad"
    scad.write_text("// stub", encoding="utf-8")
    out = tmp_path / "out.stl"
    out.write_bytes(b"\0" * 2000)
    monkeypatch.setattr(
        emboss, "generate_emboss_scad",
        lambda **_k: {"scad_path": str(scad), "output_stl_path": str(out)},
    )
    monkeypatch.setattr(
        emboss, "compile_embossed_model",
        lambda *_a, **_k: {"success": True, "stl_path": str(out), "compile_time_seconds": 0.1},
    )
    monkeypatch.setattr(emboss, "check_boolean_success", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "kiln.decoration_quota.get_decoration_quota",
        lambda: type("Q", (), {"check_and_increment": lambda self: (True, ""), "refund": lambda self: None})(),
        raising=False,
    )


def test_a_traced_mark_reports_stencil_and_mark(monkeypatch, tmp_path, dummy_stl, logo_png):
    _stub_after_content_prep(monkeypatch, tmp_path)
    monkeypatch.setenv("KILN_STAGE_PREVIEW", "0")
    result = _decorate(model_path=dummy_stl, content=logo_png, image_style="auto")
    if isinstance(result, list):
        result = next(r for r in result if isinstance(r, dict))
    assert result.get("success") is True, result.get("error")
    deco = result["decoration"]
    assert deco["image_style"] == "auto"
    assert deco["image_style_applied"] == "stencil"
    assert deco["treatment"] == "mark"
