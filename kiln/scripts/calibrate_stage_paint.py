"""Re-fit kiln.stage_paint's lighting constants against the real stage.

Run this whenever ``mesh_viewer.html`` changes its rig (lights, material,
environment, tone pipeline) and the painter must follow.  It requires a
machine that can run the photograph backend (chrome-headless-shell + a
cached stage document) — the whole point is to measure the stage, not to
guess it.

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
landed in, and it cannot drift from the code being fitted.

The sphere is photographed from TWO poses.  One from above, which is
where tops and walls live.  One from BELOW, because the four lights all
arrive from above and a downward-facing face is lit almost entirely by
``scene.environment`` — photographing only from above leaves that whole
regime to the silhouette's grazing samples, and a flat ambient constant
fitted against them put a bottom view 40 tone levels under the
photograph.

The painter's shading model is closed-form in its constants, so the fit
needs no re-rendering: coordinate descent over (key, rim, graze,
ambient, env, exposure) against the harvested tones, evaluated through
:func:`kiln.stage_paint._shade` ITSELF — no second copy of the BRDF to
drift — with the shadow end up-weighted and the two poses weighted
equally so the smaller down-facing set is not drowned.

Prints the constants to paste into ``stage_paint.py``, then renders a
probe part through BOTH backends and reports the residual so the paste
is justified by a number, not a feeling.

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
#: the camera cannot see, and its scale wandered over a 0.79..1.06 range
#: between otherwise identical fits.
_POSES = {
    "iso": (55.0, 0.0, 25.0),     # elevation +35 — tops and walls
    "under": (155.0, 0.0, 25.0),  # elevation -65 — the down-facing faces
    "back": (55.0, 0.0, 205.0),   # elevation +35, rim side — pins the rim
}

#: Surface directions closer to the silhouette than this are dropped: the
#: photograph's edge pixels are anti-aliased against the backdrop, and a
#: flat-shaded facet's normal diverges from the analytic one fastest
#: exactly there.
_MIN_NDV = 0.25

#: Samples within this many pixels of a bloom SOURCE are dropped.  The
#: stage's composer runs `UnrealBloomPass(0.45, 0.85, 0.92)` and the
#: painter models no bloom (it is a screen-space pass, not a per-pixel
#: term), so near a blown highlight the photograph carries light this
#: shading model cannot produce: measured as a monotone brightness
#: -dependent ramp reaching -76/255 at the top of the range, which an
#: unmasked fit pays for by mis-setting exposure.  0.92 HDR luma is
#: ~231/255 out of the tone curve, and the halo's lift falls under half
#: a tone level past ~30 px (measured off the backdrop, 2026-09-22).
#: The fitted constants are stable to this radius: 15, 30 and 60 px move
#: them by less than 2%.
_BLOOM_SOURCE = 231
_BLOOM_HALO_PX = 30


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


def _sample_directions(count: int) -> np.ndarray:
    """A Fibonacci sphere — even coverage, no pole clustering."""
    i = np.arange(count) + 0.5
    y = 1.0 - 2.0 * i / count
    r = np.sqrt(np.maximum(1.0 - y * y, 0.0))
    phi = i * np.pi * (1.0 + 5.0 ** 0.5)
    return np.stack([r * np.cos(phi), y, r * np.sin(phi)], axis=1)


def _bilinear(grey: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = grey.shape
    x = np.clip(x, 0, w - 1.001)
    y = np.clip(y, 0, h - 1.001)
    x0, y0 = x.astype(np.int64), y.astype(np.int64)
    tx, ty = x - x0, y - y0
    top = grey[y0, x0] * (1 - tx) + grey[y0, x0 + 1] * tx
    bot = grey[y0 + 1, x0] * (1 - tx) + grey[y0 + 1, x0 + 1] * tx
    return top * (1 - ty) + bot * ty


def _bloom_halo(grey: np.ndarray) -> np.ndarray:
    """True where the composer's bloom can reach (see ``_BLOOM_HALO_PX``).

    A box dilation of the blown pixels, run through Pillow's max filter
    rather than a distance transform so the harness needs nothing Kiln
    does not already depend on.
    """
    from PIL import Image as _Image
    from PIL import ImageFilter

    src = _Image.fromarray(((grey > _BLOOM_SOURCE) * 255).astype(np.uint8))
    # MaxFilter caps its kernel, so widen in passes rather than one jump.
    step, grown = 9, src
    for _ in range((2 * _BLOOM_HALO_PX) // (step - 1)):
        grown = grown.filter(ImageFilter.MaxFilter(step))
    return np.asarray(grown) > 0


def _harvest(png: str, label: str):
    """``(normals, views, tones)`` for one photographed pose.

    The sphere the painter draws is centred on the orbit target with
    radius ``_RADIUS`` (``try_paint_stage_views`` subtracts the bounding
    -sphere centre), so a surface direction *u* is the point ``R*u`` and
    its own normal.  Project it through the painter's camera and read the
    photograph where it landed.
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
    grey = np.asarray(Image.open(png).convert("L"), float)
    inside = (x > 1) & (x < _W - 2) & (y > 1) & (y < _H - sp._FOOTER_PX - 2)
    n, view, x, y = n[inside], view[inside], x[inside], y[inside]
    clear = ~_bloom_halo(grey)[y.astype(int), x.astype(int)]
    return n[clear], view[clear], _bilinear(grey, x[clear], y[clear])


#: ``_EXPOSURE`` is NOT fitted, and must not be: it multiplies the whole
#: accumulator just before the tone curve, so scaling it is
#: indistinguishable from scaling (_AMBIENT, _ENV_SCALE, _LIGHT_SCALES)
#: together — exactly redundant, not merely correlated.  Fitting both
#: left the search wandering a flat valley and returning a different
#: answer per run.  It is held at its shipped value and the other five
#: carry the level; nothing is lost, because the redundancy is exact.
_EXPOSURE = 0.708


def _set_constants(p) -> None:
    ks, rs, gs, amb, env = p
    sp._LIGHT_SCALES = (ks, rs, gs, gs)
    sp._AMBIENT = amb
    sp._ENV_SCALE = env
    sp._EXPOSURE = _EXPOSURE


_LUMA = np.array([0.299, 0.587, 0.114])  # what Pillow's "L" convert uses


def _model_tone(p, albedo, n, v):
    """The painter's OWN shader at trial constants — never a copy of it."""
    _set_constants(p)
    return sp._shade(albedo, n, v).astype(float) @ _LUMA


_LO = [0.0, 0.0, 0.0, 0.0, 0.0]
_HI = [3.0, 3.0, 3.0, 1.0, 6.0]
#: Several starts, best kept: coordinate descent on five correlated
#: scales can settle in a shallow side minimum, and a calibration that
#: depends on where it was started is not a measurement.
_STARTS = (
    [0.60, 2.00, 0.23, 0.05, 1.30],
    [0.20, 0.80, 0.30, 0.13, 1.60],
    [1.00, 0.30, 0.10, 0.20, 0.80],
    [0.40, 1.20, 0.40, 0.02, 2.20],
)


def _descend(err, p):
    steps = [0.2, 0.2, 0.15, 0.04, 0.3]
    best = err(p)
    for it in range(6000):
        i = it % 5
        for sign in (1, -1):
            q = list(p)
            q[i] = min(_HI[i], max(_LO[i], q[i] + sign * steps[i]))
            e = err(q)
            if e < best:
                best, p = e, q
        if it % 5 == 4:
            steps = [max(s * 0.97, 0.0008) for s in steps]
    return p, best


def _fit(albedo, n, v, tone, weight):
    def err(p):
        return float((np.abs(_model_tone(p, albedo, n, v) - tone) * weight).mean())

    runs = sorted((_descend(err, list(s)) for s in _STARTS), key=lambda r: r[1])
    spread = runs[-1][1] - runs[0][1]
    if spread > 0.05:
        print(f"  note: starts disagreed by {spread:.3f}/255 — best kept")
    return runs[0]


def _both_backends(stl: str, work: Path, p) -> list[tuple[str, float, float]]:
    """Mean model tone from the photograph and from the painter, per pose."""
    _set_constants(p)
    rows = []
    for label in _POSES:
        rot = _POSES[label]
        shot = _photograph(stl, label, work / "verify_shot")
        painted = sp.try_paint_stage_views(
            stl, [(label, label)], {label: rot},
            output_dir=str(work / "verify_paint"), width=_W, height=_H,
        )
        if not painted:
            raise SystemExit(f"the painter declined {label}")
        means = []
        for path in (shot, painted[0]["path"]):
            a = np.asarray(Image.open(path).convert("RGB"), float)
            model = np.abs(a - np.array(sp._BG, float)).sum(axis=2) > 60
            means.append(float(a.mean(axis=2)[model].mean()))
        rows.append((label, means[0], means[1]))
    return rows


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="kiln_stage_calib_"))
    stl = _sphere(work)

    albedo = sp._srgb_to_linear(
        np.array([int(sp._MODEL_COLOR.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
        / 255.0
    )

    chunks = []
    for label in _POSES:
        png = _photograph(stl, label, work / "harvest")
        n, v, tone = _harvest(png, label)
        print(f"{label}: {len(n)} samples, tone {tone.min():.0f}..{tone.max():.0f}")
        chunks.append((n, v, tone))

    # Thin to a fit-sized set: six parameters do not need 400k samples,
    # and the coordinate descent below evaluates the real shader.
    rng = np.random.default_rng(0)
    n, v, tone, weight = [], [], [], []
    for cn, cv, ct in chunks:
        take = rng.choice(len(cn), size=min(6000, len(cn)), replace=False)
        n.append(cn[take])
        v.append(cv[take])
        tone.append(ct[take])
        # Equal weight per POSE (the down-facing set is the smaller one and
        # must not be drowned), and the shadow end up-weighted inside it:
        # deep pockets on carved text extrapolate from the darkest samples,
        # and an unweighted fit let them go black while every mean looked
        # right.
        w = 1.0 + 2.0 * (ct[take] < 140)
        weight.append(w / w.mean() / len(chunks))
    n = np.vstack(n)
    v = np.vstack(v)
    tone = np.concatenate(tone)
    weight = np.concatenate(weight)

    p, best = _fit(albedo, n, v, tone, weight)
    ks, rs, gs, amb, env = p
    print(f"weighted tone err: {best:.2f}/255 over {len(n)} samples")
    for (cn, cv, ct), label in zip(chunks, _POSES, strict=True):
        resid = _model_tone(p, albedo, cn, cv) - ct
        print(f"  {label}: mean {resid.mean():+.2f}, "
              f"abs {np.abs(resid).mean():.2f}, p95 {np.percentile(np.abs(resid), 95):.2f}")

    print("paste into kiln/src/kiln/stage_paint.py:")
    print(f"  _LIGHT_SCALES = ({ks:.3f}, {rs:.3f}, {gs:.3f}, {gs:.3f})")
    print(f"  _AMBIENT = {amb:.3f}")
    print(f"  _ENV_SCALE = {env:.3f}")
    print(f"  (_EXPOSURE stays {_EXPOSURE} — see _set_constants)")

    print("both backends on the probe sphere (mean model tone):")
    for label, shot_mean, paint_mean in _both_backends(stl, work, p):
        print(f"  {label}: photograph {shot_mean:6.1f}  painter {paint_mean:6.1f}  "
              f"delta {paint_mean - shot_mean:+.1f}")


if __name__ == "__main__":
    main()
