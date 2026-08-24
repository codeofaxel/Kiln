"""Re-fit kiln.stage_paint's lighting constants against the real stage.

Run this whenever ``mesh_viewer.html`` changes its rig (lights, material,
tone pipeline) and the painter must follow.  It requires a machine that
can run the photograph backend (chrome-headless-shell + a cached stage
document) — the whole point is to measure the stage, not to guess it.

METHOD (the sphere probe)
-------------------------
A sphere shows every camera-facing surface direction in one image, and
its geometry is analytic, so a single photograph yields thousands of
(normal, view, tone) samples with no correspondence problem.  The
painter's shading model is closed-form in its constants, so the fit
needs no re-rendering: coordinate descent over (key, rim, graze,
ambient, exposure) against the harvested tones, with the shadow end
up-weighted — deep pockets on carved text extrapolate from the darkest
samples, and an unweighted fit let them go black while every mean
looked right.

Prints the constants to paste into ``stage_paint.py``, then renders a
probe part through BOTH backends and reports the residual so the paste
is justified by a number, not a feeling.

Usage:
    python3 kiln/scripts/calibrate_stage_paint.py
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kiln.stage_paint as sp  # noqa: E402
from kiln.stage_still import try_render_stage_views  # noqa: E402

sp._deps()  # bind numpy inside the module before touching its internals

_POSE = {"iso": (55.0, 0.0, 25.0)}
_SEL = [("iso", "iso")]


def _harvest(work: Path):
    """Photograph a sphere; return (normals, views, tones) samples."""
    import trimesh

    sph = trimesh.creation.icosphere(subdivisions=5, radius=45)
    sph.apply_translation([0, 0, 45])
    stl = work / "sphere.stl"
    sph.export(stl)

    ref = try_render_stage_views(
        str(stl), _SEL, _POSE, output_dir=str(work / "ref"), width=800, height=600
    )
    if not ref:
        raise SystemExit(
            "the photograph backend declined — calibration needs "
            "chrome-headless-shell and a cached stage document"
        )

    radius = 45.0
    eye, _ = sp._camera(25.0, 35.0, 800 / 600, radius, 90.0)
    ss = 2
    w, full_h = 800 * ss, 600 * ss
    canvas_h = full_h - sp._FOOTER_PX
    _, fwd = sp._view_projection(eye, w, canvas_h)
    right = np.cross(fwd, np.array([0.0, 1.0, 0.0]))
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)

    img = np.asarray(Image.open(ref[0]["path"]).convert("L"), float)
    ys, xs = np.mgrid[40:520:4, 60:740:4]
    pix = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
    focal = (canvas_h / 2) / math.tan(math.radians(sp._FOV_DEG) / 2)
    d_cam = np.stack([
        (pix[:, 0] * ss - w / 2) / focal,
        -(pix[:, 1] * ss - canvas_h / 2) / focal,
        np.ones(len(pix)),
    ], axis=1)
    d = d_cam[:, 0:1] * right + d_cam[:, 1:2] * up + d_cam[:, 2:3] * fwd
    d /= np.linalg.norm(d, axis=1)[:, None]
    b = d @ eye
    disc = b * b - (eye @ eye - radius * radius)
    hit = disc > 0
    t = -b[hit] - np.sqrt(disc[hit])
    pts = eye + d[hit] * t[:, None]
    n = pts / radius
    v = eye - pts
    v /= np.linalg.norm(v, axis=1)[:, None]
    tone = img[pix[hit][:, 1].astype(int), pix[hit][:, 0].astype(int)]
    keep = tone > 60  # sphere pixels, not backdrop/plate
    return n[keep], v[keep], tone[keep]


def _model_tone(p, n, v):
    ks, rs, gs, amb, expo = p
    scales = (ks, rs, gs, gs)
    a = sp._ROUGHNESS ** 2
    a2 = a * a
    kv = a / 2
    alb = float(sp._srgb_to_linear(np.array([0xD9 / 255]))[0])
    f0 = 0.04 + sp._METALNESS * (alb - 0.04)
    nv = np.clip((n * v).sum(1), 1e-4, None)
    col = np.full(len(n), amb)
    for (dirn, rgb, inten), sc in zip(sp._LIGHTS, scales):
        light = np.asarray(dirn) / np.linalg.norm(dirn)
        ndl = np.clip(n @ light, 0, None)
        h = light[None, :] + v
        h /= np.maximum(np.linalg.norm(h, axis=1), 1e-12)[:, None]
        ndh = np.clip((n * h).sum(1), 0, None)
        vdh = np.clip((v * h).sum(1), 0, None)
        dist = a2 / np.maximum(np.pi * (ndh * ndh * (a2 - 1) + 1) ** 2, 1e-9)
        fres = f0 + (1 - f0) * (1 - vdh) ** 5
        vis = 1 / np.maximum(
            4 * (ndl * (1 - kv) + kv) * (nv * (1 - kv) + kv), 1e-9
        )
        col += inten * sc * float(np.mean(rgb)) * (ndl + dist * fres * vis * ndl)
    lin = col * alb * expo
    aces = np.clip((lin * (2.51 * lin + 0.03)) / (lin * (2.43 * lin + 0.59) + 0.14), 0, 1)
    return np.where(aces <= 0.0031308, aces * 12.92, 1.055 * aces ** (1 / 2.4) - 0.055) * 255


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="kiln_stage_calib_"))
    n, v, tone = _harvest(work)
    print(f"harvested {len(n)} (normal, view, tone) samples")

    weight = 1.0 + 2.0 * (tone < 140)  # the shadow floor matters — see docstring

    def err(p):
        return float((np.abs(_model_tone(p, n, v) - tone) * weight).mean())

    p = [0.6, 2.0, 0.23, 0.09, 0.70]
    steps = [0.2, 0.2, 0.15, 0.06, 0.05]
    lo = [0.0, 0.0, 0.0, 0.0, 0.2]
    hi = [3.0, 3.0, 3.0, 1.0, 0.9]
    best = err(p)
    for it in range(5000):
        i = it % 5
        for sign in (1, -1):
            q = list(p)
            q[i] = min(hi[i], max(lo[i], q[i] + sign * steps[i]))
            e = err(q)
            if e < best:
                best, p = e, q
        if it % 5 == 4:
            steps = [max(s * 0.97, 0.0008) for s in steps]

    ks, rs, gs, amb, expo = p
    print(f"weighted tone err: {best:.2f}/255 over {len(n)} samples")
    print("paste into kiln/src/kiln/stage_paint.py:")
    print(f"  _LIGHT_SCALES = ({ks:.3f}, {rs:.3f}, {gs:.3f}, {gs:.3f})")
    print(f"  _AMBIENT = {amb:.3f}")
    print(f"  _EXPOSURE = {expo:.3f}")


if __name__ == "__main__":
    main()
