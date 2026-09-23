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

from unittest import mock

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


def _model_mask(a: np.ndarray) -> np.ndarray:
    """Model pixels by GEOMETRY, whatever their tone.

    The painter fills its backdrop with exactly ``_BG``, so anything else
    is part; three pixels of erosion drop the anti-aliased rim.  A tone
    threshold cannot do this job on the views that need it most: a part
    painted too dark sits right on the backdrop's colour and falls out
    of its own measurement.
    """
    from PIL import ImageFilter

    part = (np.abs(a - np.array(_BG, float)) > 2).any(axis=2)
    img = Image.fromarray((part * 255).astype(np.uint8))
    return np.asarray(img.filter(ImageFilter.MinFilter(7))) > 0


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
    mean 199.3, silhouette 553x361 at 800x600.  The painter measured
    189.4 when the environment term was fitted in (2026-09-22,
    kiln/scripts/calibrate_stage_paint.py); wide-ish tolerances absorb
    platform float noise, not a lighting regression.

    The ~10 under the photograph is the composer's bloom, which this
    backend does not model — an isometric view of this probe is nearly
    all bright top face, the one regime where the halo lands.  The
    previous calibration sat ON 199 here by running the whole rig hot
    enough to stand in for bloom, which is what put a BOTTOM view 42
    tone levels under the photograph: no light in that fit reached a
    downward face at all.  Matching here by that route is the bug, not
    the pin — see test_downward_faces_match_the_recorded_reference."""
    a = _img(_render(probe, tmp_path))
    grey = a.mean(axis=2)
    model = grey > 90
    assert abs(float(grey[model].mean()) - 189.4) < 10.0
    dist = np.abs(a - np.array(_BG, float)).sum(axis=2) > 120
    ys, xs = np.nonzero(dist)
    assert abs((xs.max() - xs.min()) - 553) <= 6
    assert abs((ys.max() - ys.min()) - 361) <= 6


def test_downward_faces_match_the_recorded_reference(
    probe: str, tmp_path: Path
) -> None:
    """The stage lights downward faces from ``scene.environment``, and
    this backend must too.

    References (browser photographs of this probe, 2026-09-22,
    chrome-headless-shell 1217, measured under this test's own mask):
    mean 98.5 from underneath and 103.4 from 45 degrees under, where the
    four directionals all arrive from above and contribute essentially
    nothing.  The painter with a flat ambient in place of the
    environment read 51.1 and 59.1 — 47 and 44 tone levels dark, a
    charcoal part where the stage shows a lit slate one."""
    for label, rot, want in (
        ("bottom", (170.0, 0.0, 0.0), 98.5),
        ("under45", (135.0, 0.0, 40.0), 103.4),
    ):
        views = try_paint_stage_views(
            probe, [(label, label)], {label: rot},
            output_dir=str(tmp_path / label), width=800, height=600,
        )
        a = _img(views)
        got = float(a.mean(axis=2)[_model_mask(a)].mean())
        assert abs(got - want) < 8.0, f"{label}: {got:.1f} against {want}"


def test_a_flat_environment_convolves_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convolution's own arithmetic, with no browser in sight.

    Both kernels are normalized weighted averages of the surrounding
    radiance, so an environment of one constant colour must come back as
    that colour from every direction — whatever the lobe.  It catches a
    mis-set measure or a normalization slip, which a tone comparison
    would only ever show as "a bit dark"."""
    stage_paint._deps()
    flat = tuple((s, (128, 128, 128)) for s, _ in stage_paint._ENV_STOPS)
    monkeypatch.setattr(stage_paint, "_ENV_STOPS", flat)
    monkeypatch.setattr(stage_paint, "_ENV_TABLES", None)
    want = float(stage_paint._srgb_to_linear(np.array([128 / 255]))[0])
    for table in stage_paint._env_tables():
        assert np.allclose(table, want, atol=1e-4)


def test_the_environment_is_brightest_overhead_and_lit_underneath() -> None:
    """The term has to VARY with the normal — that is the whole fix.

    The stage's gradient runs white at the zenith down to a lit slate at
    the nadir, so a normal's irradiance must fall monotonically from up
    to down while staying well clear of zero underneath.  A flat ambient
    (what this replaced) has ratio 1 and would fail the spread; getting
    the equirect flip backwards puts the white zenith UNDERNEATH, which
    every mean-tone check in this file would happily pass."""
    stage_paint._deps()
    diffuse, _spec = stage_paint._env_tables()
    down, horizon, up = diffuse[0].mean(), diffuse[128].mean(), diffuse[-1].mean()
    assert up > horizon > down > 0.0, (up, horizon, down)
    assert up / down > 3.0, f"too flat to be the gradient: {up / down:.2f}"
    # Underneath, the environment is the only light there is: it must be
    # worth at least as much as the flat ambient beside it.
    assert stage_paint._ENV_SCALE * down > 0.5 * stage_paint._AMBIENT


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


# ---------------------------------------------------------------------------
# Memory discipline — the pair sweep must never again scale with the scene
# ---------------------------------------------------------------------------


def test_slicing_is_math_neutral(probe: str, tmp_path: Path) -> None:
    """The sliced pair sweep is a memory shape, not a look: rendering with
    an absurdly small slice must be byte-identical to the shipped size.
    Pins the merge rule (slice-local lexsort keeps the earliest among
    depth-equals; across slices only STRICTLY nearer depth replaces) so a
    future 'optimization' that reorders ties shows up as a diff here, not
    as a subtly different product still."""
    a = _render(probe, tmp_path)
    with mock.patch.object(stage_paint, "_PAIR_SLICE", 7_001):
        out2 = tmp_path / "tiny-slice"
        b = try_paint_stage_views(
            probe, _SEL, _ISO, output_dir=str(out2), width=800, height=600,
        )
    assert (
        Path(a[0]["path"]).read_bytes() == Path(b[0]["path"]).read_bytes()
    )


def test_render_memory_stays_bounded(probe: str, tmp_path: Path) -> None:
    """A still of an ordinary probe must fit a small host.  The unsliced
    sweep laid out every (pixel, triangle) pair at once and peaked at
    5.9 GB for THIS probe at 800x600 — which OOM-killed a 2 GB production
    machine the first time a real request asked it for one thumbnail
    (2026-08-24, exit_code=137 oom_killed=true).  The bound is generous
    (2 GB) so CI variance never flakes it; the bug class it catches is an
    order of magnitude, not a margin."""
    import os
    import subprocess
    import sys

    script = (
        "import sys, resource\n"
        f"sys.path[:0] = {list(sys.path)!r}\n"
        "from kiln.stage_paint import try_paint_stage_views\n"
        f"views = try_paint_stage_views({probe!r}, [('isometric', 'i')],\n"
        "    {'isometric': (55, 0, 25)},\n"
        f"    output_dir={str(tmp_path / 'membound')!r}, width=800, height=600)\n"
        "assert views, 'painter declined the probe'\n"
        "print(int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))\n"
    )
    # A minimal, explicit environment: the child measures the PAINTER,
    # not the suite's fixtures.  The conftest relocates HOME (which would
    # orphan user-site numpy/Pillow in the child and read as "painter
    # declined"), disables the stage fetch, and other tests may leave
    # stage knobs set — any of which turns this into a test of the
    # harness.  The parent's fully-resolved sys.path is baked into the
    # script so the child sees exactly the packages the suite runs
    # against, wherever they were installed from.
    env = {"PATH": os.environ.get("PATH", "")}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=180, env=env,
    )
    assert out.returncode == 0, out.stderr[-1000:]
    peak_mb = int(out.stdout.strip().splitlines()[-1]) / (1024 * 1024)
    assert peak_mb < 2000, f"render peaked at {peak_mb:.0f} MB"


# ---------------------------------------------------------------------------
# The caller's deadline -- the one place a partial set is the honest answer
# ---------------------------------------------------------------------------

_THREE = [("isometric", "iso"), ("front", "front"), ("top", "top")]
_THREE_ROT = {
    "isometric": (55.0, 0.0, 25.0), "front": (90.0, 0.0, 0.0), "top": (0.0, 0.0, 0.0),
}


def test_a_deadline_stops_the_loop_between_views(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the caller's whole-call deadline the painter returns what fits.

    Measured 2026-09-06: with the browser declined, a six-angle 1600x1200
    paint ran ~50 s with no ceiling and pushed the tool call past the MCP
    host's window.  A view cannot be interrupted mid-raster, so the loop
    checks BEFORE each one whether another view of the last one's size
    still fits, and stops there.  The caller marks the rest as skipped.
    """
    clock = [1000.0]
    monkeypatch.setattr(stage_paint.time, "monotonic", lambda: clock[0])
    real = stage_paint._paint_view
    painted: list[int] = []

    def slow_paint(*args, cost_only=False, **kwargs):
        # The set's pre-pass prices every view through this same
        # function; only a real paint costs wall clock.
        if cost_only:
            return real(*args, cost_only=True, **kwargs)
        painted.append(1)
        clock[0] += 30.0
        return real(*args, **kwargs)

    monkeypatch.setattr(stage_paint, "_paint_view", slow_paint)
    views = try_paint_stage_views(
        probe, _THREE, _THREE_ROT, output_dir=str(tmp_path / "out"),
        width=64, height=48, deadline=clock[0] + 40.0,
    )
    # 40 s left -> iso (30 s) -> 10 s left, and the next view needs ~30 s.
    assert views is not None
    assert [v["angle"] for v in views] == ["isometric"]
    assert len(painted) == 1
    assert Path(views[0]["path"]).is_file()


def test_a_spent_deadline_paints_nothing_and_declines(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    painted: list[int] = []
    real = stage_paint._paint_view

    def counting(*a, cost_only=False, **k):
        if cost_only:
            return real(*a, cost_only=True, **k)
        painted.append(1)
        return real(*a, **k)

    monkeypatch.setattr(stage_paint, "_paint_view", counting)
    views = try_paint_stage_views(
        probe, _THREE, _THREE_ROT, output_dir=str(tmp_path / "out"),
        width=64, height=48, deadline=stage_paint.time.monotonic() - 1.0,
    )
    assert views is None
    assert painted == []


def test_no_deadline_keeps_the_all_or_nothing_set(probe: str, tmp_path: Path) -> None:
    views = try_paint_stage_views(
        probe, _THREE, _THREE_ROT, output_dir=str(tmp_path / "out"), width=64, height=48,
    )
    assert views is not None and [v["angle"] for v in views] == ["isometric", "front", "top"]


# ---------------------------------------------------------------------------
# The raster cap is a property of the SET, so it is settled before painting
# ---------------------------------------------------------------------------


def test_an_over_cap_view_declines_before_any_painting(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A view over the raster cap kills the set, so ask BEFORE painting one.

    Measured 2026-09-06 at 1600x1200, seven angles: the painter drew four
    views over ~30 s, hit the cap on the fifth, and discarded all four --
    the set is all-or-nothing, so any view over the cap dooms it from the
    start.  The pair count is arithmetic over projected triangles and
    costs nothing next to rasterizing them, so every view is priced up
    front and the whole set declines before a pixel is written.
    """
    out = tmp_path / "out"
    seen: list = []
    real_count = stage_paint._pair_count
    rasterized: list = []
    real_raster = stage_paint._rasterize

    def counted(*a, **k):
        rasterized.append(1)
        return real_raster(*a, **k)

    def second_view_is_huge(*a, **k):
        cost = real_count(*a, **k)
        seen.append(1)
        return cost._replace(total=stage_paint._PAIR_CAP + 1) if len(seen) == 2 else cost

    monkeypatch.setattr(stage_paint, "_pair_count", second_view_is_huge)
    monkeypatch.setattr(stage_paint, "_rasterize", counted)
    views = try_paint_stage_views(
        probe, _THREE, _THREE_ROT, output_dir=str(out), width=64, height=48,
    )
    assert views is None
    assert rasterized == [], "nothing may be rasterized once the set is known impossible"
    assert not list(out.glob("*.png")), "no view may reach disk"


def test_a_set_inside_the_cap_still_paints_every_view(probe: str, tmp_path: Path) -> None:
    """The pre-pass must not refuse work the rasterizer would have done."""
    views = try_paint_stage_views(
        probe, _THREE, _THREE_ROT, output_dir=str(tmp_path / "out"), width=64, height=48,
    )
    assert views is not None and len(views) == 3


def test_the_prepass_and_the_rasterizer_agree_on_cost(probe: str, tmp_path: Path) -> None:
    """One helper prices the view for both, so they can never disagree."""
    counts_seen: list = []
    real = stage_paint._pair_count

    def record(*a, **k):
        r = real(*a, **k)
        counts_seen.append(int(r.total))
        return r

    import unittest.mock as _m

    with _m.patch.object(stage_paint, "_pair_count", record):
        try_paint_stage_views(
            probe, _SEL, _ISO, output_dir=str(tmp_path / "o"), width=64, height=48,
        )
    # One pre-pass price and one rasterizer price for the single view,
    # from the same helper on the same geometry.
    assert len(counts_seen) == 2
    assert counts_seen[0] == counts_seen[1]


# ---------------------------------------------------------------------------
# A painted part is painted in its own colours
# ---------------------------------------------------------------------------
#
# Until 2026-09-22 the painter read files with trimesh, which never sees a
# 3MF's paint, so visualize_model skipped it for every painted part and a
# machine with no usable browser showed people the grey per-face render.
# It now paints the stage's own payload, colours included.


_RED, _BLUE = "#F72323", "#2366F7"


@pytest.fixture()
def painted_cube(tmp_path: Path) -> str:
    """A 20 mm cube written by Kiln's own painted-3MF writer: top and
    bottom faces red, the four sides blue."""
    trimesh = pytest.importorskip("trimesh")
    from kiln.multicolor_3mf import compose_painted_3mf

    mesh = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    mesh.apply_translation((100.0, 100.0, 10.0))
    tris = [tuple(map(tuple, mesh.vertices[f])) for f in mesh.faces]
    colors = [
        _RED if all(v[2] > 19.9 for v in t) or all(v[2] < 0.1 for v in t) else _BLUE
        for t in tris
    ]
    out = tmp_path / "painted_cube.3mf"
    compose_painted_3mf(tris, colors, output_path=str(out))
    return str(out)


def _hue_counts(a: np.ndarray) -> tuple[int, int, int]:
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = int(((r > 90) & (r > g + 50) & (r > b + 50)).sum())
    blue = int(((b > 90) & (b > r + 40) & (b > g + 20)).sum())
    green = int(((g > 90) & (g > r + 40) & (g > b + 40)).sum())
    return red, blue, green


def test_a_painted_part_is_painted_in_its_own_colours(painted_cube: str, tmp_path: Path) -> None:
    red, blue, _green = _hue_counts(_img(_render(painted_cube, tmp_path)))
    assert red > 2000, "the red top never reached the pixels"
    assert blue > 2000, "the blue sides never reached the pixels"


def test_the_bottom_view_shows_the_paint_underneath(painted_cube: str, tmp_path: Path) -> None:
    """The case that started this: paint on a face that prints against the
    plate is only seen from below, where the face is lit by ambient
    alone -- so it is judged by hue, not brightness."""
    views = try_paint_stage_views(
        painted_cube, [("bottom", "b")], {"bottom": (170, 0, 15)},
        output_dir=str(tmp_path / "o"), width=800, height=600,
    )
    a = _img(views)
    h, w = a.shape[:2]
    centre = a[h // 2 - 20: h // 2 + 20, w // 2 - 20: w // 2 + 20].reshape(-1, 3).mean(axis=0)
    r, g, b = centre
    assert r > 1.5 * g and r > 1.5 * b, f"the underside reads {centre.round()}, not red"


def test_the_parts_own_colours_outrank_a_requested_colour(painted_cube: str, tmp_path: Path) -> None:
    """The stage document's rule: a filament colour the caller assumed
    yields to the colours the user made."""
    red, blue, green = _hue_counts(_img(_render(painted_cube, tmp_path, color="#00ff00")))
    assert green == 0, "a requested colour painted over the part's own"
    assert red > 2000 and blue > 2000


def test_a_3mf_paints_without_trimeshs_3mf_loader(
    painted_cube: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain install has no lxml or networkx, which trimesh's 3MF loader
    imports when it runs.  The painter reads through the payload's own
    standard-library reader, so a 3MF still paints there."""
    import importlib.abc
    import sys

    blocked = ("lxml", "networkx")

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in blocked:
                raise ImportError(f"{name} blocked: a plain install has no {name}")
            return None

    for name in [n for n in sys.modules if n.split(".")[0] in blocked]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_Block(), *sys.meta_path])
    with pytest.raises(ImportError):
        import lxml  # noqa: F401  -- the block holds before it is relied on
    views = _render(painted_cube, tmp_path)
    assert views is not None, "a 3MF did not paint without trimesh's 3MF loader"
    red, _blue, _green = _hue_counts(_img(views))
    assert red > 2000


def test_front_door_paints_a_painted_part_in_the_stage_look(
    painted_cube: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiln import stage_still
    from kiln.model_visualizer import visualize_model

    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: None)
    r = visualize_model(
        painted_cube, angles=["isometric"], output_dir=str(tmp_path / "o"), share_link=False,
    )
    assert r["renderer"] == "stage_paint", r["renderer"]
    a = np.asarray(Image.open(r["views"][0]["path"]).convert("RGB"), float)
    red, blue, _green = _hue_counts(a)
    assert red > 1000 and blue > 1000


def test_a_painted_part_whose_colours_miss_the_payload_keeps_its_colours(
    painted_cube: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the paint does not survive into the stage payload, the part in
    one colour would be the wrong picture: the painter declines and the
    per-face renderer draws the real colours instead."""
    from kiln import local_stage, stage_still
    from kiln.model_visualizer import visualize_model

    real = local_stage._payload_for_mesh

    def colourless(*a, **k):
        payload = real(*a, **k)
        payload.pop("vertex_colors", None)
        return payload

    monkeypatch.setattr(local_stage, "_payload_for_mesh", colourless)
    monkeypatch.setattr(stage_still, "try_render_stage_views", lambda *a, **k: None)
    r = visualize_model(
        painted_cube, angles=["isometric"], output_dir=str(tmp_path / "o"), share_link=False,
    )
    assert r["renderer"] == "colored_mesh", r["renderer"]


# ---------------------------------------------------------------------------
# Shading normals — the stage's creases, corner for corner
# ---------------------------------------------------------------------------


def _prism(sides: int, radius: float = 20.0, height: float = 40.0):
    """A regular prism in the viewer frame (y up), indexed, capped."""
    ang = np.arange(sides) * (2 * np.pi / sides)
    ring = np.stack([radius * np.cos(ang), np.zeros(sides), radius * np.sin(ang)], 1)
    top = ring + [0.0, height, 0.0]
    v = np.vstack([ring, top, [[0.0, 0.0, 0.0], [0.0, height, 0.0]]])
    i = np.arange(sides)
    j = (i + 1) % sides
    lo_c, hi_c = 2 * sides, 2 * sides + 1
    # Outward winding: the painter culls and the stage creases on it.
    walls = np.concatenate([np.stack([i, sides + i, j], 1),
                            np.stack([j, sides + i, sides + j], 1)])
    caps = np.concatenate([np.stack([np.full(sides, lo_c), i, j], 1),
                           np.stack([np.full(sides, hi_c), sides + j, sides + i], 1)])
    return v, np.concatenate([walls, caps]).astype(np.int64), len(walls)


def _wall_spread(sides: int) -> float:
    """Largest gap between the normals the corners at one wall vertex carry.

    Measured as the distance between unit vectors, not an angle: arccos
    near 1 turns a float32 rounding step into a hundredth of a degree.
    """
    stage_paint._deps()
    v, f, n_wall = _prism(sides)
    cn = stage_paint._creased_normals(v, f)[:n_wall].reshape(-1, 3).astype(float)
    corners = f[:n_wall].reshape(-1)
    worst = 0.0
    for vid in np.unique(corners):
        at = cn[corners == vid]
        gap = np.linalg.norm(at[:, None, :] - at[None, :, :], axis=2)
        worst = max(worst, float(gap.max()))
    return worst


def test_walls_round_off_under_thirty_degrees_and_stay_hard_over_it() -> None:
    """The stage creases at 30 degrees, and this must crease where it does.

    A 13-gon's facets meet at 27.7 degrees, so its wall shades round: every
    corner at a wall vertex carries the same normal.  An 11-gon's meet at
    32.7, so it stays faceted, each corner keeping its own face's normal
    (the full 32.7 apart).  Moving the crease either way breaks one side."""
    assert _wall_spread(13) < 1e-6
    assert abs(_wall_spread(11) - 2 * np.sin(np.radians(360.0 / 11) / 2)) < 1e-6


def test_a_60gon_joins_its_seams_and_keeps_its_rims() -> None:
    """The case the stage's own parity tests pin: a 60-gon prism.

    Across every wall seam the facets agree on one normal, level, and
    lying BETWEEN the two facets (each 3 degrees off the radial) -- not
    exactly radial, because the mean is per triangle, as three's is, and
    here one facet brings two triangles to a ring vertex and its
    neighbour one.  At the rim, where wall meets cap at 90 degrees, the
    cap keeps a straight up or down normal instead of rounding the edge."""
    stage_paint._deps()
    v, f, n_wall = _prism(60)
    cn = stage_paint._creased_normals(v, f).astype(float)
    assert _wall_spread(60) < 1e-6  # one normal per seam
    wall = cn[:n_wall].reshape(-1, 3)
    radial = v[f[:n_wall].reshape(-1)] * [1.0, 0.0, 1.0]
    radial /= np.linalg.norm(radial, axis=1)[:, None]
    off = np.degrees(np.arccos(np.clip((wall * radial).sum(axis=1), -1.0, 1.0)))
    assert np.allclose(wall[:, 1], 0.0, atol=1e-6)
    assert off.max() < 3.0 - 1e-3, f"a seam normal left its facets: {off.max():.3f} deg"
    caps = cn[n_wall:]
    assert np.allclose(np.abs(caps[..., 1]), 1.0, atol=1e-6)
    assert np.allclose(caps[..., [0, 2]], 0.0, atol=1e-6)


def test_a_round_wall_paints_without_facet_steps(tmp_path: Path) -> None:
    """Through the public door: a 60-gon's wall is shaded as a curve.

    Measured 2026-09-22 against the stage that creases: across the wall,
    tone moves at most 0.64 per pixel in the photograph and 0.66 here;
    the flat-shaded painter stepped 4.0 at every facet edge."""
    trimesh = pytest.importorskip("trimesh")
    cyl = trimesh.creation.cylinder(radius=20, height=40, sections=60)
    cyl.apply_translation([0, 0, 20])
    src = tmp_path / "cyl60.stl"
    cyl.export(src)
    views = try_paint_stage_views(
        str(src), [("front", "f")], {"front": (90.0, 0.0, 0.0)},
        output_dir=str(tmp_path / "o"), width=800, height=600,
    )
    grey = _img(views).mean(axis=2)
    wall = grey > 100
    ys, _xs = np.nonzero(wall)
    mid = int(np.median(ys))
    band = slice(mid - 25, mid + 25)
    cols = np.nonzero(wall[band].all(axis=0))[0]
    profile = grey[band, cols.min() + 10:cols.max() - 10].mean(axis=0)
    assert np.abs(np.diff(profile)).max() < 2.0


def test_normals_a_payload_ships_are_the_ones_painted(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage creases only a payload that ships no normals; one that
    ships them is drawn with them, and so is it here."""
    import base64

    from kiln import local_stage

    real = local_stage._payload_for_mesh

    def facing_up(*a, **k):
        payload = real(*a, **k)
        n = len(base64.b64decode(payload["positions"])) // 12
        up = np.tile(np.array([0.0, 1.0, 0.0], dtype="<f4"), n)
        payload["normals"] = base64.b64encode(up.tobytes()).decode("ascii")
        return payload

    own = _img(_render(probe, tmp_path / "own", plate=False))
    monkeypatch.setattr(local_stage, "_payload_for_mesh", facing_up)
    shipped = _img(_render(probe, tmp_path / "shipped", plate=False))
    # Every face lit as a top face: the walls' shading is gone, so the
    # part's tones collapse toward one value.
    spread = lambda a: float(a.mean(axis=2)[_model_mask(a)].std())  # noqa: E731
    assert spread(shipped) < 0.5 * spread(own)


def test_a_fan_past_the_crease_budget_declines(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A part whose crease pass would stall the preview falls through,
    like one past the face cap — never a half-shaded picture."""
    monkeypatch.setattr(stage_paint, "_CREASE_MAX_PAIRS", 10)
    assert _render(probe, tmp_path) is None
