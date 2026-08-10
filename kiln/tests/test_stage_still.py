"""Stage-still backend: capability detection, fallback honesty, wiring.

The backend's one contract is that it NEVER breaks a preview: every
precondition miss returns ``None`` and ``visualize_model`` runs its
OpenSCAD path untouched.  These tests pin both halves — the happy path
through a fake browser, and each individual way the backend must stand
aside.  A real-browser render is exercised separately (kiln-pro's
fidelity test drives the actual bundle); here the browser is a script,
so the suite runs everywhere.
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from kiln import stage_still
from kiln.stage_still import (
    _frame_ok,
    _openscad_rotation_to_orbit,
    find_browser,
    try_render_stage_views,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake-browser scripts are POSIX"
)


# ---------------------------------------------------------------------------
# Fixtures: a mesh, a still-capable stage doc, fake browsers
# ---------------------------------------------------------------------------

@pytest.fixture()
def cube_stl(tmp_path: Path) -> str:
    import trimesh

    path = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(path))
    return str(path)


@pytest.fixture()
def stage_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    doc = tmp_path / "stage.html"
    doc.write_text(
        "<!doctype html><html><head></head><body><p>stage __KILN_STILL__</p>"
        "</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setenv("KILN_STAGE_DOC", str(doc))
    return doc


def _make_fake_browser(tmp_path: Path, body: str) -> Path:
    """An executable python script standing in for chromium."""
    script = tmp_path / "fake_browser.py"
    script.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _textured_png_bytes() -> bytes:
    """A small PNG with real luminance variation (passes the blank guard)."""
    import io
    import random

    from PIL import Image

    rng = random.Random(7)
    im = Image.new("L", (64, 64))
    im.putdata([rng.randrange(0, 256) for _ in range(64 * 64)])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def good_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    png = tmp_path / "frame.png"
    png.write_bytes(_textured_png_bytes())
    script = _make_fake_browser(
        tmp_path,
        f"""
        import shutil, sys
        out = [a for a in sys.argv if a.startswith("--screenshot=")][0]
        shutil.copy({str(png)!r}, out.split("=", 1)[1])
        # Capture the harness CONTENT now — the engine deletes its temp
        # dir before returning, so the path alone would be useless.
        page = [a for a in sys.argv if a.startswith("file://")][0]
        body = open(page[len("file://"):]).read()
        with open({str(tmp_path / "seen_harnesses.txt")!r}, "a") as f:
            f.write(body + "\\n===HARNESS===\\n")
        """,
    )
    monkeypatch.setenv("KILN_STAGE_BROWSER", str(script))
    return script


@pytest.fixture()
def blank_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = _make_fake_browser(
        tmp_path,
        """
        import sys
        from PIL import Image
        out = [a for a in sys.argv if a.startswith("--screenshot=")][0]
        Image.new("L", (64, 64), 26).save(out.split("=", 1)[1])
        """,
    )
    monkeypatch.setenv("KILN_STAGE_BROWSER", str(script))
    return script


_VIEWS = [("isometric", "3/4 overview"), ("front", "front face")]
_ROTATIONS = {"isometric": (55, 0, 25), "front": (90, 0, 0)}


# ---------------------------------------------------------------------------
# Unit: angle mapping and the blank-frame guard
# ---------------------------------------------------------------------------

def test_rotation_mapping_covers_the_canonical_angles() -> None:
    # el = 90 - rx, az = rz: top-down rx=0 → el 90; horizontal rx=90 → el 0;
    # under-view rx=170 → el -80.  These are the exact rotations
    # model_visualizer has always fed OpenSCAD.
    assert _openscad_rotation_to_orbit(55, 25) == (25.0, 35.0)
    assert _openscad_rotation_to_orbit(90, 0) == (0.0, 0.0)
    assert _openscad_rotation_to_orbit(15, 10) == (10.0, 75.0)
    assert _openscad_rotation_to_orbit(170, 15) == (15.0, -80.0)


def test_blank_guard_rejects_flat_and_accepts_textured(tmp_path: Path) -> None:
    from PIL import Image

    flat = tmp_path / "flat.png"
    Image.new("L", (64, 64), 26).save(flat)
    assert not _frame_ok(str(flat), 64, 64)

    textured = tmp_path / "textured.png"
    textured.write_bytes(_textured_png_bytes())
    assert _frame_ok(str(textured), 64, 64)

    assert not _frame_ok(str(tmp_path / "absent.png"), 64, 64)


# ---------------------------------------------------------------------------
# Capability detection: every honest "no"
# ---------------------------------------------------------------------------

def test_opt_out_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_NO_STAGE_STILLS", "1")
    assert find_browser() is None


def test_explicit_override_that_is_wrong_does_not_scan_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KILN_NO_STAGE_STILLS", raising=False)
    monkeypatch.setenv("KILN_STAGE_BROWSER", "/nonexistent/browser")
    assert find_browser() is None


def test_no_browser_returns_none(
    cube_stl: str, stage_doc: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KILN_STAGE_BROWSER", "/nonexistent/browser")
    assert (
        try_render_stage_views(
            cube_stl, _VIEWS, _ROTATIONS,
            output_dir=str(tmp_path), width=64, height=64,
        )
        is None
    )


def test_doc_without_still_marker_returns_none(
    cube_stl: str, good_browser: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = tmp_path / "old_stage.html"
    doc.write_text("<!doctype html><html><body>old</body></html>", encoding="utf-8")
    monkeypatch.setenv("KILN_STAGE_DOC", str(doc))
    assert (
        try_render_stage_views(
            cube_stl, _VIEWS, _ROTATIONS,
            output_dir=str(tmp_path), width=64, height=64,
        )
        is None
    )


def test_unreadable_mesh_returns_none(
    stage_doc: Path, good_browser: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "not_a_mesh.stl"
    bad.write_text("hello", encoding="utf-8")
    assert (
        try_render_stage_views(
            str(bad), _VIEWS, _ROTATIONS,
            output_dir=str(tmp_path), width=64, height=64,
        )
        is None
    )


# ---------------------------------------------------------------------------
# The render path itself
# ---------------------------------------------------------------------------

def test_happy_path_renders_every_view(
    cube_stl: str, stage_doc: Path, good_browser: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    views = try_render_stage_views(
        cube_stl, _VIEWS, _ROTATIONS,
        output_dir=str(out), width=64, height=64,
    )
    assert views is not None
    assert [v["angle"] for v in views] == ["isometric", "front"]
    for v in views:
        assert os.path.getsize(v["path"]) > 0

    # The browser was pointed at a harness page carrying the still config —
    # the payload and pose travel as data, never as injected behaviour.
    harnesses = [
        h for h in
        (tmp_path / "seen_harnesses.txt").read_text().split("===HARNESS===")
        if h.strip()
    ]
    assert len(harnesses) == 2
    assert "__KILN_STILL__" in harnesses[0]
    assert '"az_deg": 25.0' in harnesses[0]


def test_blank_frame_falls_back(
    cube_stl: str, stage_doc: Path, blank_browser: Path, tmp_path: Path
) -> None:
    # The browser "works" — exit 0, file written — but the stage stayed
    # empty.  This is the half-working browser the guard exists for.
    assert (
        try_render_stage_views(
            cube_stl, _VIEWS, _ROTATIONS,
            output_dir=str(tmp_path), width=64, height=64,
        )
        is None
    )


# ---------------------------------------------------------------------------
# visualize_model wiring
# ---------------------------------------------------------------------------

def test_visualize_model_uses_stage_views_and_labels_renderer(
    cube_stl: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = [
        {"angle": "isometric", "description": "3/4 overview", "path": str(tmp_path / "a.png")}
    ]
    (tmp_path / "a.png").write_bytes(_textured_png_bytes())
    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: canned)

    from kiln.model_visualizer import visualize_model

    result = visualize_model(cube_stl, angles=["isometric"], output_dir=str(tmp_path))
    assert result["success"] is True
    assert result["renderer"] == "stage"
    assert result["views"] == canned


def test_visualize_model_custom_color_skips_stage_backend(
    cube_stl: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stage ignores caller colors, so a colored render must never
    # route through it — even on a machine where the backend would work.
    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("stage backend must not run for colored renders")

    monkeypatch.setattr(stage_still, "try_render_stage_views", _boom)

    from kiln.model_visualizer import visualize_model

    result = visualize_model(
        cube_stl, angles=["isometric"], output_dir=str(tmp_path), color="#FF0000"
    )
    # Whether OpenSCAD is installed or not, the call must not have hit the
    # stage backend; renderer is openscad on success and the envelope is
    # still honest on failure.
    if result.get("success"):
        assert result["renderer"] == "openscad"


def test_visualize_model_falls_back_when_stage_declines(
    cube_stl: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: None)

    from kiln.model_visualizer import visualize_model

    result = visualize_model(cube_stl, angles=["isometric"], output_dir=str(tmp_path))
    if result.get("success"):
        assert result["renderer"] == "openscad"
