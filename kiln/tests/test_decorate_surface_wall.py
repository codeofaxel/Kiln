"""decorate_surface ``face="wall"`` — curved-wall text contract.

The wall path wraps text around a detected upright round wall (cups,
vases, bowls) and is engine-delegated: the wrap engine ships with
kiln-pro, so this suite pins the contract that must hold in a PUBLIC-ONLY
install — input validation fires before any engine import (text-only,
deboss-only), and a missing engine yields the honest ENGINE_UNAVAILABLE
pointer to the hosted service instead of a crash or a wrong-face carve.
The success path runs only where kiln-pro and OpenSCAD are both present
(dev machines; the kiln-pro suite owns its behaviour in depth).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile

import pytest

from kiln.server import decorate_surface

_decorate = getattr(decorate_surface, "fn", decorate_surface)


def _err(result) -> dict:
    """The error block of a decorate_surface failure dict."""
    assert isinstance(result, dict), f"expected error dict, got {type(result)}"
    assert result.get("success") is False, result
    return result.get("error") or {}


@pytest.fixture()
def dummy_stl(tmp_path):
    """A minimal file with an .stl extension — validation-order tests only
    (the wall branch's content/mode checks fire before any mesh parsing)."""
    p = tmp_path / "body.stl"
    # One-triangle binary STL: header + count + 1 record.
    with open(p, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", 1))
        fh.write(struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0))
    return str(p)


@pytest.fixture()
def tiny_png(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    p = tmp_path / "logo.png"
    pil.new("RGB", (4, 4), (10, 10, 10)).save(p)
    return str(p)


@pytest.fixture()
def photo_png(tmp_path):
    """A continuous-tone image — a gradient, no strokes to trace."""
    pil = pytest.importorskip("PIL.Image")
    p = tmp_path / "photo.png"
    img = pil.new("L", (64, 64))
    img.putdata([(x * 4) % 256 for y in range(64) for x in range(64)])
    img.save(p)
    return str(p)


@pytest.fixture()
def mark_png(tmp_path):
    """A bi-level mark: a dark frame on white."""
    pil = pytest.importorskip("PIL.Image")
    p = tmp_path / "mark.png"
    img = pil.new("L", (64, 64), 255)
    px = img.load()
    for x in range(8, 56):
        for y in range(8, 56):
            if x < 14 or x >= 50 or y < 14 or y >= 50:
                px[x, y] = 0
    img.save(p)
    return str(p)


@pytest.fixture()
def mark_svg(tmp_path):
    p = tmp_path / "mark.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
        '<path d="M5 5 H95 V55 H5 Z M15 15 H85 V45 H15 Z" fill="#000" fill-rule="evenodd"/>'
        "</svg>"
    )
    return str(p)


def test_wall_refuses_photo_relief_with_a_pointer(dummy_stl, photo_png):
    """Photos have no strokes to trace and heightmap relief cannot wrap a
    curve honestly — the wall path says so instead of carving a smear."""
    err = _err(_decorate(model_path=dummy_stl, content=photo_png, face="wall"))
    assert err.get("code") == "INVALID_CONTENT"
    msg = err.get("message", "").lower()
    assert "line-art" in msg or "bi-level" in msg
    assert "lid" in msg or "flat" in msg


def test_wall_rejects_qr_content(dummy_stl):
    err = _err(_decorate(model_path=dummy_stl, content="qr:https://kiln3d.com", face="wall"))
    assert err.get("code") == "INVALID_CONTENT"


class _FakeWallEngine:
    """Records what the door hands the engines, returns a canned carve."""

    class NoRoundWallError(ValueError):
        pass

    class MarkDoesNotFitError(ValueError):
        def __init__(self, verdict):
            self.verdict = verdict
            super().__init__("no fit")

    def __init__(self, tmp_path, *, refuse=None):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, dict]] = []
        self.refuse = refuse

    def plan_mark_on_mesh_wall(self, model_path, **kw):
        self.calls.append(("plan", {"model_path": model_path, **kw}))
        return {"width_mm": 30.0, "aspect_ratio": kw["aspect_ratio"]}

    def wrap_mark_on_mesh_wall(self, model_path, content_info=None, **kw):
        self.calls.append(("wrap", {"model_path": model_path, "content_info": content_info, **kw}))
        if self.refuse is not None:
            raise self.refuse
        out = self.tmp_path / "wrapped.stl"
        out.write_bytes(b"")
        if kw.get("prepare") is not None:
            kw["prepare"](30.0)
        return {
            "stl_path": str(out), "scad_path": str(self.tmp_path / "w.scad"),
            "width_mm": 30.0, "height_mm": 18.0, "wrapped_deg": 62.5,
            "radius_mm": 27.5, "z_mm": 21.5, "wall_slope": 0.0,
            "plain_band_mm": [0.2, 42.8], "warnings": ["engine note"],
            "decoration_faces": {"count": 42},
            "meta": {"depth_mm": 0.8},
        }

    def wrap_text_on_mesh_wall(self, *a, **kw):  # pragma: no cover - not exercised
        raise AssertionError("text engine must not be called for a mark")


def _install_engine(monkeypatch, engine):
    import types

    fake_bridge = types.ModuleType("kiln_pro.bridge")
    fake_bridge.pro_features = types.SimpleNamespace(wall_text=engine)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", fake_bridge)
    # The success tail composes previews / quota tiles; keep it inert here.
    import kiln.server as srv

    monkeypatch.setattr(srv, "_finish_decoration_result", lambda d, *, content: d)


def _ok(result) -> dict:
    if isinstance(result, list):
        result = next(i for i in result if isinstance(i, dict) and "success" in i)
    assert result.get("success") is True, result
    return result


def test_wall_bilevel_image_routes_to_the_mark_engine(dummy_stl, mark_png, monkeypatch, tmp_path):
    engine = _FakeWallEngine(tmp_path)
    _install_engine(monkeypatch, engine)
    result = _ok(_decorate(model_path=dummy_stl, content=mark_png, face="wall", depth_mm=0.8, scale=0.6))
    kinds = [k for k, _ in engine.calls]
    assert kinds == ["wrap"], engine.calls
    call = engine.calls[0][1]
    assert call["content_info"]["openscad_polygons"], "the mark was not traced"
    assert call["width_fraction"] == pytest.approx(0.6)
    assert call["depth_mm"] == 0.8
    assert result["face"]["name"] == "wall"
    assert result["face"]["plain_band_mm"] == [0.2, 42.8]
    assert result["decoration"]["content_type"] == "image"
    assert result["decoration"]["width_mm"] == 30.0
    assert result["decoration_faces"] == {"count": 42}
    assert "engine note" in result["warnings"]


def test_wall_svg_plans_then_prepares_at_the_final_width(dummy_stl, mark_svg, monkeypatch, tmp_path):
    """SVG stroke floors scale with physical size, so the artwork is
    prepared AFTER the engine has sized it."""
    engine = _FakeWallEngine(tmp_path)
    _install_engine(monkeypatch, engine)
    result = _ok(_decorate(model_path=dummy_stl, content=mark_svg, face="wall", absolute_size_mm=25.0))
    kinds = [k for k, _ in engine.calls]
    assert kinds == ["plan", "wrap"], engine.calls
    plan_call = engine.calls[0][1]
    assert plan_call["aspect_ratio"] == pytest.approx(0.6, abs=0.05)
    assert plan_call["target_width_mm"] == 25.0
    wrap_call = engine.calls[1][1]
    assert wrap_call["content_info"] is None and callable(wrap_call["prepare"])
    assert wrap_call["plan"]["width_mm"] == 30.0
    assert result["decoration"]["content_type"] == "svg"


def test_wall_mark_that_does_not_fit_reports_the_verdict(dummy_stl, mark_png, monkeypatch, tmp_path):
    verdict = {"fits": False, "warnings": ["too small"], "suggestions": ["use a lid"]}
    engine = _FakeWallEngine(tmp_path, refuse=_FakeWallEngine.MarkDoesNotFitError(verdict))
    _install_engine(monkeypatch, engine)
    result = _decorate(model_path=dummy_stl, content=mark_png, face="wall")
    err = _err(result)
    assert err.get("code") == "MARK_DOES_NOT_FIT"
    assert result.get("suggestions") == ["use a lid"]


def test_wall_mark_on_a_box_is_no_round_wall(dummy_stl, mark_png, monkeypatch, tmp_path):
    engine = _FakeWallEngine(tmp_path, refuse=_FakeWallEngine.NoRoundWallError("no wall"))
    _install_engine(monkeypatch, engine)
    err = _err(_decorate(model_path=dummy_stl, content=mark_png, face="wall"))
    assert err.get("code") == "NO_ROUND_WALL"


def test_wall_is_deboss_only(dummy_stl):
    err = _err(
        _decorate(
            model_path=dummy_stl, content="text:HI", face="wall", mode="emboss"
        )
    )
    assert err.get("code") == "INVALID_MODE"


def test_wall_rejects_empty_text(dummy_stl):
    """'text:' with nothing after it is a content problem, and must say so —
    not be reported as a wrong-shaped model."""
    err = _err(_decorate(model_path=dummy_stl, content="text:", face="wall"))
    assert err.get("code") == "INVALID_CONTENT"


def test_wall_rejects_obj_with_a_format_error(tmp_path):
    """OBJ passes the generic model check (the flat path accepts it) but the
    wall path needs an STL — the refusal must name the real reason instead
    of blaming the model's shape."""
    obj = tmp_path / "cup.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    err = _err(_decorate(model_path=str(obj), content="text:HI", face="wall"))
    assert err.get("code") == "UNSUPPORTED_FORMAT"


def test_wall_without_engine_points_at_hosted_service(dummy_stl, monkeypatch):
    """Public-only install: the wall path degrades to an honest pointer,
    never a crash and never a silent flat-face carve."""
    import types

    fake_bridge = types.ModuleType("kiln_pro.bridge")
    fake_bridge.pro_features = None
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", fake_bridge)
    err = _err(_decorate(model_path=dummy_stl, content="text:HI", face="wall"))
    assert err.get("code") == "ENGINE_UNAVAILABLE"
    assert "api.kiln3d.com" in err.get("message", "")


def test_wall_mark_without_mark_engine_points_at_hosted_service(dummy_stl, mark_png, monkeypatch):
    """An older kiln-pro with only the text engine: a mark on the wall gets
    the same honest pointer, never a crash into a missing attribute."""
    import types

    fake_bridge = types.ModuleType("kiln_pro.bridge")
    fake_bridge.pro_features = types.SimpleNamespace(
        wall_text=types.SimpleNamespace(wrap_text_on_mesh_wall=lambda *a, **k: None)
    )
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", fake_bridge)
    err = _err(_decorate(model_path=dummy_stl, content=mark_png, face="wall"))
    assert err.get("code") == "ENGINE_UNAVAILABLE"


def _openscad_available() -> bool:
    try:
        subprocess.run(
            ["openscad", "--version"], capture_output=True, check=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _openscad_available(), reason="needs OpenSCAD")
def test_wall_success_wraps_text_on_a_cup():
    """Full path where the engine is present: a real cup gets real wrapped
    text, the response reports the wall placement, and the output exists."""
    pytest.importorskip("kiln_pro.wall_text")

    with tempfile.TemporaryDirectory() as td:
        scad = os.path.join(td, "cup.scad")
        stl = os.path.join(td, "cup.stl")
        with open(scad, "w") as fh:
            fh.write(
                "difference() { cylinder(r=40, h=80, $fn=120); "
                "translate([0,0,3]) cylinder(r=37, h=80, $fn=120); }"
            )
        subprocess.run(
            ["openscad", "-q", "-o", stl, "--export-format", "binstl", scad],
            check=True,
            capture_output=True,
            timeout=120,
        )
        result = _decorate(
            model_path=stl, content="text:KILN", face="wall", depth_mm=0.8
        )
        # attach_inspect_bundle may wrap the dict in content blocks.
        if isinstance(result, list):
            result = next(
                item
                for item in result
                if isinstance(item, dict) and "success" in item
            )
        assert result.get("success") is True, result
        assert result["face"]["name"] == "wall"
        assert result["face"]["radius_mm"] == pytest.approx(40.0, abs=0.5)
        assert os.path.isfile(result["output_stl"])
        # The response reports what was CARVED, not what was requested.
        deco = result["decoration"]
        assert deco["depth_mm"] == pytest.approx(0.8, abs=0.01)
        assert deco["text_size_mm"] > 0


@pytest.mark.slow
@pytest.mark.skipif(not _openscad_available(), reason="needs OpenSCAD")
def test_wall_honours_sizing_parameters():
    """``scale`` and ``absolute_size_mm`` must actually change the letters —
    they were previously echoed back while being ignored."""
    pytest.importorskip("kiln_pro.wall_text")

    with tempfile.TemporaryDirectory() as td:
        scad, stl = os.path.join(td, "c.scad"), os.path.join(td, "c.stl")
        with open(scad, "w") as fh:
            fh.write(
                "difference() { cylinder(r=40, h=80, $fn=120); "
                "translate([0,0,3]) cylinder(r=37, h=80, $fn=120); }"
            )
        subprocess.run(
            ["openscad", "-q", "-o", stl, "--export-format", "binstl", scad],
            check=True, capture_output=True, timeout=120,
        )

        def size_of(**kw):
            r = _decorate(model_path=stl, content="text:HI", face="wall", **kw)
            if isinstance(r, list):
                r = next(
                    i for i in r if isinstance(i, dict) and "success" in i
                )
            return r["decoration"]["text_size_mm"]

        assert size_of(scale=0.2) < size_of(), "scale must shrink the text"
        assert size_of(absolute_size_mm=9.0) == pytest.approx(9.0, abs=0.01)


@pytest.mark.slow
@pytest.mark.skipif(not _openscad_available(), reason="needs OpenSCAD")
def test_wall_success_wraps_a_mark_on_a_cup(mark_png):
    """Full path where the engine is present: a real cup gets a real wrapped
    mark with face provenance recorded beside it."""
    pytest.importorskip("kiln_pro.wall_mark")
    with tempfile.TemporaryDirectory() as td:
        scad, stl = os.path.join(td, "cup.scad"), os.path.join(td, "cup.stl")
        with open(scad, "w") as fh:
            fh.write(
                "difference() { cylinder(r=40, h=80, $fn=120); "
                "translate([0,0,3]) cylinder(r=37, h=80, $fn=120); }"
            )
        subprocess.run(
            ["openscad", "-q", "-o", stl, "--export-format", "binstl", scad],
            check=True, capture_output=True, timeout=120,
        )
        result = _ok(_decorate(model_path=stl, content=mark_png, face="wall", depth_mm=0.8))
        assert result["face"]["name"] == "wall"
        assert result["face"]["radius_mm"] == pytest.approx(40.0, abs=0.5)
        assert result["decoration"]["content_type"] == "image"
        assert result["decoration"]["depth_mm"] == pytest.approx(0.8, abs=0.01)
        assert result["decoration"]["width_mm"] > 6.0
        assert os.path.isfile(result["output_stl"])
        assert os.path.isfile(result["output_stl"] + ".decoration_faces.json")
