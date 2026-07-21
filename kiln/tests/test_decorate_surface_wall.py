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


def test_wall_rejects_non_text_content(dummy_stl, tiny_png):
    err = _err(_decorate(model_path=dummy_stl, content=tiny_png, face="wall"))
    assert err.get("code") == "INVALID_CONTENT"
    assert "text" in err.get("message", "").lower()


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
