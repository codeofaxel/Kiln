"""An SVG mark on a flat face is a decoration like a photo or text.

Every plan's decorations include SVG, Free's monthly allowance too, so a
plain install never refuses an SVG for a plan, whoever is signed in, on a
flat face as on a curved wall.  Walked through ``decorate_surface`` on a
plain install.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">'
    '<rect x="2" y="2" width="36" height="16" fill="black"/></svg>'
)


def _box(path: Path) -> str:
    import trimesh

    box = trimesh.creation.box(extents=(60.0, 40.0, 6.0))
    box.apply_translation((30.0, 20.0, 3.0))
    box.export(path)
    return str(path)


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """No kiln-pro, and no sign-in on this machine deciding anything."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", None)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    return tmp_path


def _refusal_of(res: object) -> dict:
    if isinstance(res, list):
        res = next(r for r in res if isinstance(r, dict))
    assert isinstance(res, dict)
    return res


def test_an_svg_on_a_flat_face_is_never_refused_for_a_plan(plain_install):
    import kiln.server as server

    logo = plain_install / "logo.svg"
    logo.write_text(_SVG)
    res = _refusal_of(server.decorate_surface(
        model_path=_box(plain_install / "box.stl"), content=str(logo), face="top",
        mode="emboss", depth_mm=1.0,
    ))
    error = res.get("error")
    code = error.get("code") if isinstance(error, dict) else res.get("code")
    message = error.get("message", "") if isinstance(error, dict) else str(error or "")
    assert code != "PRO_REQUIRED", res
    assert "needs Kiln Pro" not in message and "kiln3d.com/pricing" not in message, res


@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_an_svg_on_a_flat_face_is_carved_on_a_plain_install(plain_install):
    import kiln.server as server

    logo = plain_install / "logo.svg"
    logo.write_text(_SVG)
    res = _refusal_of(server.decorate_surface(
        model_path=_box(plain_install / "box.stl"), content=str(logo), face="top",
        mode="emboss", depth_mm=1.0,
    ))
    assert res.get("success") is True, res
    assert Path(res["output_stl"]).is_file()
