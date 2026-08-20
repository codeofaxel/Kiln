"""The software stage painter — parity contracts and the bugs it must not regrow.

Two kinds of pins.  CONTRACT: the backend behaves like its photograph
sibling (all-or-nothing, silent declines, same opt-out, same letterbox
geometry).  CALIBRATION: the output still matches the recorded reference
statistics measured against real browser photographs of the probe part
(2026-08-18, chrome-headless-shell 1217, stage doc with the twin-graze
rig), so drift from the stage look is caught by CI rather than felt by a
user.  The tolerances are wide enough for cross-platform float noise and
narrow enough that a lighting, framing, or letterbox regression trips.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kiln import stage_paint
from kiln.stage_paint import try_paint_stage_views

_BG = (26, 34, 45)  # #1A222D
_ISO = {"isometric": (55.0, 0.0, 25.0)}
_SEL = [("isometric", "iso")]


@pytest.fixture(autouse=True)
def _stage_family_live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KILN_NO_STAGE_STILLS", raising=False)


@pytest.fixture()
def probe(tmp_path: Path) -> str:
    """The calibration part: a coaster-like disc with a raised notch.

    Same construction the reference statistics were recorded against —
    trimesh primitives, 45 mm disc, 40 mm notch standing proud so hidden
    -surface resolution is exercised (the notch VANISHED under the
    disc's cap triangles in the painter's-algorithm prototype).
    """
    trimesh = pytest.importorskip("trimesh")
    cyl = trimesh.creation.cylinder(radius=45, height=8, sections=96)
    cyl.apply_translation([0, 0, 4])
    box = trimesh.creation.box(extents=[40, 40, 4])
    box.apply_translation([0, 0, 8])
    out = tmp_path / "probe.stl"
    trimesh.util.concatenate([cyl, box]).export(out)
    return str(out)


@pytest.fixture()
def tiny_stl(tmp_path: Path) -> str:
    stl = tmp_path / "tri.stl"
    tri = (
        struct.pack("<fff", 0, 0, 1)
        + struct.pack("<fff", 0, 0, 0)
        + struct.pack("<fff", 20, 0, 0)
        + struct.pack("<fff", 0, 20, 0)
        + struct.pack("<H", 0)
    )
    stl.write_bytes(b"\x00" * 80 + struct.pack("<I", 1) + tri)
    return str(stl)


def _render(src: str, tmp_path: Path, **kw):
    out = tmp_path / "out"
    return try_paint_stage_views(
        src, _SEL, _ISO, output_dir=str(out), width=800, height=600, **kw
    )


def _img(views) -> np.ndarray:
    return np.asarray(Image.open(views[0]["path"]).convert("RGB"), float)


# ---------------------------------------------------------------------------
# Contract — the photograph sibling's rules, kept
# ---------------------------------------------------------------------------


def test_paints_every_requested_view(probe: str, tmp_path: Path) -> None:
    sel = [("front", "f"), ("top", "t"), ("isometric", "i")]
    rots = {"front": (90, 0, 0), "top": (0, 0, 0), "isometric": (55, 0, 25)}
    views = try_paint_stage_views(
        probe, sel, rots, output_dir=str(tmp_path / "o"), width=800, height=600
    )
    assert views is not None
    assert [v["angle"] for v in views] == ["front", "top", "isometric"]
    for v in views:
        assert os.path.getsize(v["path"]) > 1000


def test_opt_out_env_disables_the_painter(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KILN_NO_STAGE_STILLS", "1")
    assert _render(probe, tmp_path) is None


def test_a_non_hex_colour_declines(probe: str, tmp_path: Path) -> None:
    assert _render(probe, tmp_path, color="tomato") is None


def test_a_hex_colour_tints_the_model(probe: str, tmp_path: Path) -> None:
    views = _render(probe, tmp_path, color="#cc3311")
    a = _img(views)
    # model pixels: bright and warm; the tint must actually arrive
    model = a[..., 0] > 100
    assert model.any()
    r, g = a[..., 0][model].mean(), a[..., 1][model].mean()
    assert r > g + 30, "requested red tint never reached the pixels"


def test_an_unreadable_mesh_declines(tmp_path: Path) -> None:
    bad = tmp_path / "nope.stl"
    bad.write_bytes(b"not a mesh")
    assert _render(str(bad), tmp_path) is None


def test_the_face_cap_declines_rather_than_downgrades(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stage_paint, "_MAX_FACES", 4)
    assert _render(probe, tmp_path) is None


def test_missing_dependencies_decline_silently(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stage_paint, "_deps", lambda: None)
    assert _render(probe, tmp_path) is None


def test_determinism_two_runs_byte_equal(probe: str, tmp_path: Path) -> None:
    a = try_paint_stage_views(
        probe, _SEL, _ISO, output_dir=str(tmp_path / "a"), width=800, height=600
    )
    b = try_paint_stage_views(
        probe, _SEL, _ISO, output_dir=str(tmp_path / "b"), width=800, height=600
    )
    assert Path(a[0]["path"]).read_bytes() == Path(b[0]["path"]).read_bytes()


def test_shares_the_photograph_backends_pose_mapping() -> None:
    """One orbit mapping for both stage backends — imported, never copied."""
    import inspect

    src = inspect.getsource(stage_paint.try_paint_stage_views)
    assert "from kiln.stage_still import" in src
    assert "_openscad_rotation_to_orbit" in src


# ---------------------------------------------------------------------------
# Calibration — measured against real browser photographs (2026-08-18)
# ---------------------------------------------------------------------------


def test_background_is_the_stage_background(probe: str, tmp_path: Path) -> None:
    a = _img(_render(probe, tmp_path))
    assert tuple(a[2, 2].astype(int)) == _BG
    assert tuple(a[2, -3].astype(int)) == _BG


def test_the_footer_letterbox_matches_the_photograph(
    probe: str, tmp_path: Path
) -> None:
    """The still page reserves 56 CSS px under the canvas; at the default
    2x supersample that is a flat 28-row strip on an 800x600 still.  The
    photograph has it, so the painting must -- the two backends have to
    be geometrically interchangeable."""
    a = _img(_render(probe, tmp_path))
    # The Lanczos downscale blends 1-2 rows at the canvas/footer boundary;
    # the strip's interior must be flat page background.
    strip = a[-24:, :, :]
    assert (np.abs(strip - np.array(_BG)) < 3).all(), "footer strip not flat bg"
    above = a[-60:-32, :, :]
    assert (np.abs(above - np.array(_BG)).sum(axis=2) > 10).any(), (
        "content should reach the canvas bottom edge"
    )


def test_model_tone_matches_the_recorded_reference(
    probe: str, tmp_path: Path
) -> None:
    """Reference (browser photograph, same probe, same pose): model-region
    mean 199, silhouette 553x361 at 800x600.  The painter measured 197.9
    on the day the sphere-probe calibration was fitted
    (kiln/scripts/calibrate_stage_paint.py); wide-ish tolerances absorb
    platform float noise, not a lighting regression."""
    a = _img(_render(probe, tmp_path))
    grey = a.mean(axis=2)
    model = grey > 90
    assert abs(float(grey[model].mean()) - 198.0) < 10.0
    dist = np.abs(a - np.array(_BG, float)).sum(axis=2) > 120
    ys, xs = np.nonzero(dist)
    assert abs((xs.max() - xs.min()) - 553) <= 6
    assert abs((ys.max() - ys.min()) - 361) <= 6


def test_hidden_surfaces_resolve_the_notch_stays_visible(
    probe: str, tmp_path: Path
) -> None:
    """The painter's-algorithm prototype drew the disc cap OVER the notch
    standing on it (a face large relative to the scene sorts by a
    centroid that misrepresents it locally).  The z-buffer must keep the
    notch's shadowed side walls visible: dark model pixels well below
    the cap tone, inside the model silhouette."""
    a = _img(_render(probe, tmp_path))
    grey = a.mean(axis=2)
    model = grey > 90
    cap = float(grey[model].mean())
    ys, xs = np.nonzero(model)
    cy, cx = int(ys.mean()), int(xs.mean())
    centre = grey[cy - 60 : cy + 60, cx - 90 : cx + 90]
    assert (centre < cap - 25).any(), "notch side walls lost to mis-ordering"


def test_bottom_view_shows_no_plate(probe: str, tmp_path: Path) -> None:
    """The bed is a FrontSide plane: from underneath it does not exist."""
    views = try_paint_stage_views(
        probe, [("bottom", "b")], {"bottom": (170, 0, 0)},
        output_dir=str(tmp_path / "o"), width=800, height=600,
    )
    a = _img(views)
    # The model sits centred; from below, the four corners can hold
    # nothing but page background -- a visible plate would lay grid
    # lines through them.  (Checking corners rather than every
    # non-model pixel keeps the model's anti-aliased halo out of the
    # verdict.)
    dev = np.abs(a - np.array(_BG, float)).sum(axis=2)
    h, w = dev.shape
    for patch in (dev[:100, :100], dev[:100, -100:],
                  dev[-128:-28, :100], dev[-128:-28, -100:]):
        assert (patch < 10).all(), "plate leaked into a bottom-view corner"


def test_front_door_uses_the_painter_when_the_photograph_declines(
    tiny_stl: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiln import stage_still
    from kiln.model_visualizer import visualize_model

    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: None)
    r = visualize_model(
        tiny_stl, angles=["isometric"], output_dir=str(tmp_path / "o"),
        share_link=False,
    )
    assert r["success"] is True
    assert r["renderer"] == "stage_paint"


def test_a_mesh_preview_needs_no_openscad_when_the_stage_serves(
    tiny_stl: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenSCAD is resolved only for views the stage did not draw.

    The front door used to refuse with OPENSCAD_NOT_FOUND before trying
    either stage backend, so a machine with the painter's deps but no
    OpenSCAD got nothing at all — the exact machine the painter exists
    for (CI reproduced it on every run).  A mesh served entirely by the
    stage must never touch OpenSCAD; a pure-SCAD input, which no stage
    backend can read, must still get the honest refusal.
    """
    from kiln import model_visualizer as mv
    from kiln import stage_still

    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: None)

    def _no_openscad() -> str:
        raise FileNotFoundError("OpenSCAD not found (simulated)")

    monkeypatch.setattr(mv, "_find_openscad", _no_openscad)

    r = mv.visualize_model(
        tiny_stl, angles=["isometric"], output_dir=str(tmp_path / "o"),
        share_link=False,
    )
    assert r["success"] is True
    assert r["renderer"] == "stage_paint"

    scad = tmp_path / "cube.scad"
    scad.write_text("cube([10, 10, 10]);")
    r2 = mv.visualize_model(
        str(scad), angles=["isometric"], output_dir=str(tmp_path / "o2"),
        share_link=False,
    )
    assert r2["success"] is False
    assert r2["code"] == "OPENSCAD_NOT_FOUND"

# ---------------------------------------------------------------------------
# The close-camera plate, and the orientation the photograph pinned
# ---------------------------------------------------------------------------


def _orange(a: np.ndarray) -> np.ndarray:
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (r > g + 6) & (g > b + 1) & (r - b > 12) & (r < 170)


def test_small_parts_keep_the_plate_near_plane_clip(tmp_path: Path) -> None:
    """A 30mm part orbits the camera ~60mm out, which puts the 256mm
    plate's near corners BEHIND the camera.  The first cut skipped the
    whole plate when any corner failed the depth test -- a small part
    rendered against bare backdrop with no bed, no grid, no ember cross
    (Adam caught it in a showcase).  The plate must clip, not vanish."""
    trimesh = pytest.importorskip("trimesh")
    disc = trimesh.creation.cylinder(radius=15, height=4, sections=64)
    disc.apply_translation([0, 0, 2])
    src = tmp_path / "small.stl"
    disc.export(src)
    views = try_paint_stage_views(
        str(src), _SEL, _ISO, output_dir=str(tmp_path / "o"),
        width=800, height=600,
    )
    a = _img(views)
    assert int(_orange(a).sum()) > 300, "ember cross missing: plate was culled"


def test_plate_texture_centre_cross_uses_js_rounding() -> None:
    """The canvas puts the cross at Math.round(25/2)=13; Python's
    banker's round(12.5)=12 shifted it a full cell."""
    stage_paint._deps()
    tex = np.asarray(stage_paint._plate_texture(None).convert("RGB"), float)
    cross = _orange(tex)
    cols = np.nonzero(cross.any(axis=0))[0]
    cell = stage_paint._CELL_MM * stage_paint._PX_PER_MM
    vertical = [c for c in cols if abs(c - 13 * cell) <= 3]
    assert vertical, f"vertical ember line not at cell 13 (cols near: {cols[:10]})"


def test_plate_orientation_matches_the_photograph(tmp_path: Path, probe: str) -> None:
    """The marker experiment, pinned: at the steep pose the KILN stamp's
    densest orange cluster landed in the LEFT half of the browser
    photograph (recorded 2026-08-18, x~100 of 800).  The painted plate
    must keep that orientation -- a mirrored texture axis passes every
    symmetric-part test and quietly ships a flipped bed."""
    views = try_paint_stage_views(
        probe, [("steep", "s")], {"steep": (30, 0, 70)},
        output_dir=str(tmp_path / "o"), width=800, height=600,
    )
    a = _img(views)
    o = _orange(a)
    ys, xs = np.nonzero(o)
    assert len(xs) > 0
    h = np.zeros((a.shape[0] // 40 + 1, a.shape[1] // 40 + 1))
    np.add.at(h, (ys // 40, xs // 40), 1)
    j, i = np.unravel_index(h.argmax(), h.shape)
    assert i * 40 < 400, f"stamp cluster at x~{i*40}: plate orientation flipped"


def test_every_dependency_the_painter_needs_is_a_core_dependency() -> None:
    """The painter's soft imports must be declared, or it is inert on install.

    ``_deps()`` returns None when any of numpy, Pillow or trimesh is missing,
    and declining is silent by design -- the caller just gets the OpenSCAD
    look.  So an undeclared dependency does not fail anywhere; the feature
    simply never runs, for everyone who installed the documented way.

    That shipped: on 1.4.1 Pillow was only in the ``emboss`` extra, so a clean
    ``pip install kiln3d`` rendered ``renderer="openscad"`` and installing
    Pillow alone flipped the same call to ``renderer="stage_paint"``.  numpy
    and trimesh had each been promoted to core for this identical reason.
    """
    import re

    pyproject = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", pyproject)
    assert block, "could not find the dependencies array in kiln/pyproject.toml"
    declared = block.group(1).lower()

    for dist in ("numpy", "pillow", "trimesh"):
        assert re.search(rf'"{dist}[><=]', declared), (
            f"kiln.stage_paint imports {dist} but it is not a core dependency. "
            "The painter declines SILENTLY when an import is missing, so this "
            "does not fail a test anywhere -- it just makes the studio-look "
            "preview inert on a default install."
        )


# ---------------------------------------------------------------------------
# Card knobs — plate=False / letterbox=False (library-thumbnail surfaces)
# ---------------------------------------------------------------------------


def test_plate_off_paints_no_bed(probe: str, tmp_path: Path) -> None:
    """``plate=False`` removes the bed entirely — grid lines, ember cross,
    stamp, and the contact shadow its texture carries — so everything
    outside the part's silhouette is the bare backdrop.  With the plate on,
    grid lines break the backdrop all over the lower half; without it, the
    off-model region must be uniform."""
    with_plate = _img(_render(probe, tmp_path, plate=True, letterbox=False))
    out2 = tmp_path / "o2"
    no_plate = np.asarray(
        Image.open(
            try_paint_stage_views(
                probe, _SEL, _ISO,
                output_dir=str(out2), width=800, height=600,
                plate=False, letterbox=False,
            )[0]["path"]
        ).convert("RGB"),
        float,
    )
    def off_model_variation(a: np.ndarray) -> float:
        # Everything darker than the model: backdrop + plate region.
        grey = a.mean(axis=2)
        region = a[grey < 90]
        return float(region.std(axis=0).max())

    assert off_model_variation(no_plate) < off_model_variation(with_plate)
    # And absolutely: the bed-less ground is flat backdrop (gradient-free
    # within a couple of counts), which no grid line survives.
    grey = no_plate.mean(axis=2)
    ground = no_plate[grey < 90]
    assert ground.std(axis=0).max() < 3.0, "plate chrome leaked into ground"


def test_letterbox_off_fills_the_full_height(probe: str, tmp_path: Path) -> None:
    """``letterbox=False`` hands back the footer rows: the bottom strip is
    no longer reserved flat page background, so content (the plate under
    the default look) reaches the bottom edge."""
    a = _img(_render(probe, tmp_path, letterbox=False))
    assert a.shape[:2] == (600, 800)
    strip = a[-24:, :, :]
    # Under the default (plate on) look the plate's grid reaches the
    # bottom rows — the strip is NOT uniformly flat page background.
    assert (np.abs(strip - np.array(_BG)).sum(axis=2) > 10).any(), (
        "letterbox=False still reserved a flat footer strip"
    )


def test_card_knobs_default_on_keeps_the_stage_contract(
    probe: str, tmp_path: Path
) -> None:
    """No caller passes the knobs → byte-identical to the pre-knob look
    (the calibration pins above run knobless, but pin the default
    explicitly so a default flip can't hide behind them)."""
    a = _render(probe, tmp_path)
    out2 = tmp_path / "explicit"
    b = try_paint_stage_views(
        probe, _SEL, _ISO, output_dir=str(out2), width=800, height=600,
        plate=True, letterbox=True,
    )
    assert (
        Path(a[0]["path"]).read_bytes() == Path(b[0]["path"]).read_bytes()
    )
