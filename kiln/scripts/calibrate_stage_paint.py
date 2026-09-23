"""Check kiln.stage_paint against photographs of the real stage.

Run this whenever ``mesh_viewer.html`` changes its rig (lights, material,
environment, composer) and the painter must follow.  It requires a
machine that can run the photograph backend (chrome-headless-shell + a
cached stage document) — the whole point is to measure the stage, not to
guess it.

NOTHING IS FITTED
-----------------
The painter's constants are transcriptions of the page and of three r160,
so this script does not produce constants to paste.  It produces two
verdicts:

1. PHOTOGRAPH AGAINST PAINTING.  The probe sphere is photographed and
   painted from the same poses, and both images are read at the same
   surface points.  The residual is reported per tone band, because the
   failure this exists to catch is a residual that GROWS WITH TONE: that
   is what an ACES curve the stage does not apply looked like (the
   brightest band 45-88 levels dark), and what a highlight tinted by the
   part's colour looked like.

2. A FREE FIT, as a transcription check.  Each light term's level is
   fitted against the photographs and reported as a multiple of its
   transcribed value; every one should land near 1.00.  One that does not
   names the term whose transcription has drifted from the page.  The fit
   sees every sample, highlight cores included: the painter's own bloom
   is captured from its render of the same pose and added in linear light
   before the tone step, exactly where the composer adds it, so no region
   has to be masked out.

METHOD (the sphere probe)
-------------------------
A sphere shows every camera-facing surface direction in one image, and
its geometry is analytic, so a single photograph yields thousands of
(normal, view, tone) samples with no correspondence problem.  Samples
are placed by FORWARD-projecting points on the sphere through the
painter's own camera (:func:`kiln.stage_paint._view_projection`) rather
than by re-deriving a ray per pixel: the two backends are geometrically
interchangeable by contract, so the painter's projection is the honest
way to ask which pixel of the photograph a given surface direction
landed in, and it cannot drift from the code being checked.

Three poses: one from above (tops and walls), one from BELOW (the four
lights all arrive from above, so a downward face is lit by
``scene.environment`` alone), and one from the rim light's side.

Usage:
    python3 kiln/scripts/calibrate_stage_paint.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kiln.stage_paint as sp  # noqa: E402
from kiln.stage_still import (  # noqa: E402
    _openscad_rotation_to_orbit,
    try_render_stage_views,
)

sp._deps()  # bind numpy inside the module before touching its internals

_W, _H = 800, 600
_RADIUS = 45.0

#: OpenSCAD camera rotations, the caller's own spelling (rx tilts from
#: straight-down: 0 top, 90 horizontal, 170 under).  ``under`` sits at
#: elevation -65, inside the stage's own [-1.2, 1.45] rad clamp, so the
#: pose the harness reconstructs is the pose the browser actually shot.
#: ``back`` faces the opposite azimuth because the rim light comes from
#: -x/-z: photographed only from the front, the rim lights mostly what
#: the camera cannot see.
_POSES = {
    "iso": (55.0, 0.0, 25.0),     # elevation +35 — tops and walls
    "under": (155.0, 0.0, 25.0),  # elevation -65 — the down-facing faces
    "back": (55.0, 0.0, 205.0),   # elevation +35, rim side — pins the rim
}

#: Surface directions closer to the silhouette than this are dropped: the
#: photograph's edge pixels are anti-aliased against the backdrop.
_MIN_NDV = 0.25

_BANDS = (0, 60, 100, 140, 180, 210, 230, 245, 256)
_LUMA = np.array([0.299, 0.587, 0.114])  # what Pillow's "L" convert uses


def _sphere(work: Path) -> str:
    import trimesh

    sph = trimesh.creation.icosphere(subdivisions=5, radius=_RADIUS)
    sph.apply_translation([0, 0, _RADIUS])  # stand it on the bed
    stl = work / "sphere.stl"
    sph.export(stl)
    return str(stl)


def _photograph(stl: str, label: str, out: Path) -> str:
    rot = _POSES[label]
    for _attempt in range(4):  # the browser's first shot can lose the race
        got = try_render_stage_views(
            stl, [(label, label)], {label: rot},
            output_dir=str(out), width=_W, height=_H,
        )
        if got:
            return got[0]["path"]
    raise SystemExit(
        f"the photograph backend declined {label} — calibration needs "
        "chrome-headless-shell and a cached stage document"
    )


def _paint(stl: str, label: str, out: Path):
    """The painter's still of *label*, and the bloom field it added."""
    captured = {}
    real = sp._unreal_bloom

    def spy(hdr, device_size):
        bloom = real(hdr, device_size)
        captured["bloom"] = bloom
        return bloom

    sp._unreal_bloom = spy
    try:
        painted = sp.try_paint_stage_views(
            stl, [(label, label)], {label: _POSES[label]},
            output_dir=str(out), width=_W, height=_H,
        )
    finally:
        sp._unreal_bloom = real
    if not painted:
        raise SystemExit(f"the painter declined {label}")
    return painted[0]["path"], captured.get("bloom")


def _sample_directions(count: int) -> np.ndarray:
    """A Fibonacci sphere — even coverage, no pole clustering."""
    i = np.arange(count) + 0.5
    y = 1.0 - 2.0 * i / count
    r = np.sqrt(np.maximum(1.0 - y * y, 0.0))
    phi = i * np.pi * (1.0 + 5.0 ** 0.5)
    return np.stack([r * np.cos(phi), y, r * np.sin(phi)], axis=1)


def _bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    x = np.clip(x, 0, w - 1.001)
    y = np.clip(y, 0, h - 1.001)
    x0, y0 = x.astype(np.int64), y.astype(np.int64)
    tx, ty = x - x0, y - y0
    if img.ndim == 3:
        tx, ty = tx[:, None], ty[:, None]
    top = img[y0, x0] * (1 - tx) + img[y0, x0 + 1] * tx
    bot = img[y0 + 1, x0] * (1 - tx) + img[y0 + 1, x0 + 1] * tx
    return top * (1 - ty) + bot * ty


def _samples(label: str):
    """``(normals, views, x_out, y_out, x_int, y_int)`` for one pose.

    The sphere the painter draws is centred on the orbit target with
    radius ``_RADIUS`` (``try_paint_stage_views`` subtracts the bounding
    -sphere centre), so a surface direction *u* is the point ``R*u`` and
    its own normal.  Projected through the painter's camera at its
    internal resolution, then scaled to the output still.
    """
    az, el = _openscad_rotation_to_orbit(_POSES[label][0], _POSES[label][2])
    # The painter's internal frame, its own arithmetic (see
    # try_paint_stage_views): supersample, then a footer strip whose
    # CSS height is scaled to the internal resolution.
    from kiln.preview_render import effective_supersample

    ss = effective_supersample()
    ss_int = min(ss + 1, 4)
    canvas_h = _H * ss_int - round(sp._FOOTER_PX * ss_int / ss)

    eye, _dist = sp._camera(az, el, _W / _H, _RADIUS, 2 * _RADIUS)
    project, _fwd = sp._view_projection(eye, _W * ss_int, canvas_h)

    n = _sample_directions(400_000)
    pts = n * _RADIUS
    view = eye[None, :] - pts
    view /= np.linalg.norm(view, axis=1)[:, None]
    facing = (n * view).sum(axis=1) > _MIN_NDV  # convex: facing == visible
    n, view, pts = n[facing], view[facing], pts[facing]

    px, py, _pz = project(pts)
    x, y = px / ss_int, py / ss_int  # the letterbox pastes the canvas at (0,0)
    inside = (x > 1) & (x < _W - 2) & (y > 1) & (y < _H - sp._FOOTER_PX - 2)
    return n[inside], view[inside], x[inside], y[inside], px[inside], py[inside]


def _tone(path: str, x, y) -> np.ndarray:
    """Luma at continuous image coordinates (a pixel's centre is at +0.5)."""
    rgb = np.asarray(Image.open(path).convert("RGB"), float)
    return _bilinear(rgb, x - 0.5, y - 0.5) @ _LUMA


def _band_report(label: str, got: np.ndarray, ref: np.ndarray) -> float:
    """Print the residual per tone band; return the worst band's |mean|."""
    cells, worst = [], 0.0
    for lo, hi in zip(_BANDS[:-1], _BANDS[1:], strict=True):
        sel = (ref >= lo) & (ref < hi)
        if sel.sum() >= 30:
            mean = float((got[sel] - ref[sel]).mean())
            worst = max(worst, abs(mean))
            cells.append(f"{lo}-{hi}: {mean:+.1f}")
    print(f"  {label}: mean {np.mean(got - ref):+.2f}, abs {np.mean(np.abs(got - ref)):.2f} | "
          + "  ".join(cells))
    return worst


# The fitted terms, as multiples of their transcriptions.
_TERMS = ("key", "rim", "graze", "ambient", "env")


def _apply(p) -> None:
    key, rim, graze, ambient, env = p
    base = _TRANSCRIBED
    lights = list(base["lights"])
    for i, scale in enumerate((key, rim, graze, graze)):
        direction, colour, intensity = lights[i]
        lights[i] = (direction, colour, intensity * scale)
    sp._LIGHTS = tuple(lights)
    sp._AMBIENT = (base["ambient"][0], base["ambient"][1] * ambient)
    sp._ENV_INTENSITY = base["env"] * env


_TRANSCRIBED = {"lights": sp._LIGHTS, "ambient": sp._AMBIENT, "env": sp._ENV_INTENSITY}


def _model_tone(p, albedo, n, v, bloom):
    """The painter's OWN shader and tone step at trial levels."""
    _apply(p)
    lin = sp._shade(albedo, n, v) + bloom
    return sp._linear_to_srgb(lin) * 255.0 @ _LUMA


def _fit(albedo, chunks):
    """Coordinate descent from the transcription, every sample weighted."""
    def err(p):
        return float(np.mean([
            np.abs(_model_tone(p, albedo, n, v, b) - t).mean() for n, v, b, t in chunks
        ]))

    p = [1.0] * len(_TERMS)
    best = err(p)
    steps = [0.1] * len(_TERMS)
    for it in range(60 * len(_TERMS)):
        i = it % len(_TERMS)
        for sign in (1, -1):
            q = list(p)
            q[i] = max(0.0, q[i] + sign * steps[i])
            e = err(q)
            if e < best:
                best, p = e, q
        if i == len(_TERMS) - 1:
            steps = [max(s * 0.8, 0.002) for s in steps]
    _apply([1.0] * len(_TERMS))
    return p, best


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="kiln_stage_calib_"))
    stl = _sphere(work)
    albedo = sp._srgb_to_linear(
        np.array([int(sp._MODEL_COLOR.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
        / 255.0
    )

    print("photograph against painting, residual (painter - photograph) per tone band:")
    rng = np.random.default_rng(0)
    chunks, worst = [], 0.0
    for label in _POSES:
        shot = _photograph(stl, label, work / "shot")
        painted, bloom = _paint(stl, label, work / "paint")
        n, v, x, y, xi, yi = _samples(label)
        ref = _tone(shot, x, y)
        worst = max(worst, _band_report(label, _tone(painted, x, y), ref))
        lift = (np.zeros((len(n), 3)) if bloom is None
                else _bilinear(bloom.astype(np.float64), xi - 0.5, yi - 0.5))
        take = rng.choice(len(n), size=min(6000, len(n)), replace=False)
        chunks.append((n[take], v[take], lift[take], ref[take]))
    print(f"worst band: {worst:.1f} tone levels")

    p, best = _fit(albedo, chunks)
    print(f"free fit, every sample (mean abs {best:.2f}/255); each term as a multiple "
          "of its transcription -- all should sit near 1.00:")
    for name, value in zip(_TERMS, p, strict=True):
        print(f"  {name:8s} x{value:.3f}")


if __name__ == "__main__":
    main()
