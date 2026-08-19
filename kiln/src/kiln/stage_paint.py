"""Stage-look still renders painted in software — no browser, no GL, no GPU.

WHY THIS EXISTS
---------------
:mod:`kiln.stage_still` photographs the real three.js stage through a
headless browser, and on macOS the only browser that can do that without
bouncing a Dock icon is Playwright's ``chrome-headless-shell`` — a binary
essentially no real install has (Playwright is not a Kiln dependency and
nothing in Kiln downloads browsers).  So on the machines that matter the
photograph path declines, and until this module existed every still fell
all the way back to the OpenSCAD look: a competent render that shares
nothing with the product's calibrated stage.

This module paints the SAME stage — the ``#1A222D`` backdrop, the print
bed with its 10 mm grid and ember centre-cross, the contact shadow, the
four-light rig — with numpy and Pillow, both of which Kiln already
depends on.  It is not a new look; every constant below is transcribed
from the one authority, ``mesh_viewer.html`` (the document the browser
path photographs), with the transcription source noted inline.  When the
stage document changes its rig, this module must follow — the constants
carry their source line context so the diff is mechanical.

WHERE IT SITS
-------------
``visualize_model``'s backend chain, in order of fidelity:

1. ``stage_still`` — a photograph of the stage itself.  Pixel-exact.
2. ``stage_paint`` (this) — the stage repainted in software.  Same
   geometry, same rig, approximated shading.
3. OpenSCAD — the always-available floor.

Everything here is best-effort and silent, exactly like the photograph
path: any miss returns ``None`` and the caller falls through.  The same
``KILN_NO_STAGE_STILLS=1`` opt-out disables both stage-look backends —
it means "give me the OpenSCAD look", not "avoid browsers".

WHAT IS APPROXIMATED, HONESTLY
------------------------------
No bloom pass, and the light rig's OUTPUT levels are fitted rather
than transcribed: the _LIGHTS intensities are three.js-internal units
that do not survive three's physically-scaled pipeline into pixel
values, so _LIGHT_SCALES / _AMBIENT / _EXPOSURE are measured off real
photographs by ``kiln/scripts/calibrate_stage_paint.py`` -- the
sphere-probe method documented there; re-run it whenever the stage
document's rig changes.  The BRDF itself is not approximated: real GGX
with Schlick Fresnel and Smith visibility, the MeshPhysicalMaterial's
own lobe.  Flat shading is parity, not a shortcut: the payload ships
no normals by design and the stage flat-shades stills ("matching the
faceted look of Kiln's OpenSCAD previews" -- mesh_payload).  Hidden
surfaces are resolved by a true z-buffer with perspective-correct
interpolation, so composed and interpenetrating bodies
(``compose_models`` output) draw correctly.  The camera math, plate,
palette, and letterbox are transcribed, not approximated.  Calibration
tests pin the output against recorded reference statistics so drift
from the stage look is caught, not felt.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["try_paint_stage_views"]

# --- the rig, transcribed from mesh_viewer.html ---------------------------
# Background: `scene.background = new THREE.Color(0x1a222d)` and the
# body CSS `background: #1A222D` ("flat by design").
_BG = (0x1A, 0x22, 0x2D)

# Material: `MeshPhysicalMaterial({ color: 0xd9d9d9, roughness: 0.4,
# metalness: 0.05 })` — materials.ts "default".
_MODEL_COLOR = "#d9d9d9"
_ROUGHNESS = 0.4

# Lights: `AmbientLight(0xffffff, 0.35)`, key `0xfff7ee @ 1.0` from
# (10, 20, 10), rim `0xd8e1ff @ 0.5` from (-15, 8, -10), graze
# `0xffffff @ 0.75` from (16, 6, 1.5), counter-graze `0xffffff @ 0.5`
# from (-16, 6, 1.5).  Positions are directions (normalized in-scene).
_AMBIENT = 0.085  # fitted: the transcribed 0.35 is a three-internal unit
_LIGHTS = (
    # (direction xyz, color rgb 0..1, intensity)
    ((10.0, 20.0, 10.0), (1.0, 0xF7 / 0xFF, 0xEE / 0xFF), 1.0),
    ((-15.0, 8.0, -10.0), (0xD8 / 0xFF, 0xE1 / 0xFF, 1.0), 0.5),
    ((16.0, 6.0, 1.5), (1.0, 1.0, 1.0), 0.75),
    ((-16.0, 6.0, 1.5), (1.0, 1.0, 1.0), 0.5),
)

# Camera: `PerspectiveCamera(35, ...)`; still framing
# `orbit.fitRadius * (STILL.dist_factor || 3.4)` clamped to the orbit
# bounds fitCameraToStage derives; elevation clamped to [-1.2, 1.45] rad.
_FOV_DEG = 35.0
_STILL_DIST_FACTOR = 3.4
_EL_CLAMP = (-1.2, 1.45)

# Plate: DEFAULT_PLATE_MM = 256, texture at 4 px/mm, 10 mm cells; the
# bed plane sits at `floorY - 0.2`; a FrontSide plane, so it is invisible
# from underneath — a bottom view shows the model against bare backdrop.
_PLATE_MM = 256.0
_PX_PER_MM = 4
_CELL_MM = 10
_PLATE_BASE = (31, 31, 31, int(0.45 * 255))
_GRID_MINOR = (89, 89, 89, int(0.30 * 255))
_GRID_CENTRE = (255, 107, 43, int(0.18 * 255))
_RIM = (102, 102, 102, int(0.35 * 255))
_STAMP = (255, 107, 43, int(0.18 * 255))

# Contact shadow blob: radius `max(dx, dz) * 0.55 + 4`, radial gradient
# alpha stops 0.5 / 0.26 / 0 (inner radius 8/64 of the canvas).
_BLOB_STOPS = ((0.0, 0.5), (0.5, 0.26), (1.0, 0.0))

# The still page reserves a 56 CSS-px footer under the canvas
# (`#stage.fill { height: calc(100vh - 56px); }`), so every photograph
# is a (w x h-56) canvas letterboxed over the page background.  Painted
# identically so the two stage backends are geometrically interchangeable
# -- a machine that has the browser and one that does not must produce
# the same framing.
_FOOTER_PX = 56

# Payload bounds, matching the photograph path's honesty rule: a mesh too
# big to paint faithfully falls through rather than shipping a downgrade.
_MAX_FACES = 600_000

_OPT_OUT_ENV = "KILN_NO_STAGE_STILLS"

# Exposure trim: the browser still is tone-mapped by three's OutputPass;
# this scalar is the one fitted constant (calibrated against reference
# stills of the probe cube, see test_stage_paint) rather than a
# transcription.  It absorbs the difference between three's light-unit
# conventions and the plain N·L sum below.
_EXPOSURE = 0.708

_METALNESS = 0.05  # MeshPhysicalMaterial metalness, transcribed

#: Per-light output scales (key, rim, graze, counter-graze), fitted the
#: same way.  The _LIGHTS intensities are transcribed three.js-internal
#: units; three's physically-scaled pipeline does not sum them the way a
#: plain N-dot-L does, and the visible casualty was WALL tone: the
#: grazes arrive near-horizontal, a naive sum let them flood every
#: vertical wall, and carved text lost the wall/top contrast that makes
#: it read (Adam: "significantly less crispy").  Fitted so wall and top
#: tones both match the photograph.
_LIGHT_SCALES = (0.601, 1.970, 0.234, 0.234)


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:  # noqa: F821
    return _np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:  # noqa: F821
    c = _np.clip(c, 0.0, 1.0)
    return _np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _aces(x: np.ndarray) -> np.ndarray:  # noqa: F821
    """Narkowicz's ACES filmic fit — the curve three.js applies."""
    return _np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0)


_np = None  # populated by _deps(); module import stays dependency-free


def _deps():
    """Import the soft dependencies, or explain which one is missing."""
    global _np
    try:
        import numpy as np
        import trimesh  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError as exc:
        logger.debug("stage paint unavailable: %s", exc)
        return None
    _np = np
    return np


def _load_viewer_frame_mesh(file_path: str):
    """Triangles in the stage's y-up frame, or ``None``.

    The payload's baked rotation (``mesh_payload``): (x, y, z)_mesh →
    (x, z, -y)_viewer.  Baking it here the same way keeps the light rig
    and orbit mapping verbatim rather than mirrored.
    """
    import trimesh

    try:
        mesh = trimesh.load(file_path, force="mesh")
    except Exception as exc:  # noqa: BLE001 — any unreadable source → decline
        logger.debug("stage paint: cannot read %s: %s", file_path, exc)
        return None
    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        return None
    if len(mesh.faces) > _MAX_FACES:
        logger.debug(
            "stage paint: %d faces exceeds the %d cap", len(mesh.faces), _MAX_FACES
        )
        return None
    v = _np.asarray(mesh.vertices, dtype=_np.float64)
    f = _np.asarray(mesh.faces, dtype=_np.int64)
    v = _np.column_stack([v[:, 0], v[:, 2], -v[:, 1]])
    return v, f


def _bounding_sphere(v):
    """three.js ``computeBoundingSphere``: bbox centre, max vertex distance."""
    lo, hi = v.min(axis=0), v.max(axis=0)
    c = (lo + hi) / 2.0
    r = float(_np.sqrt(((v - c) ** 2).sum(axis=1).max()))
    return c, max(r, 1e-6), lo, hi


def _camera(az_deg: float, el_deg: float, aspect: float, fit_radius: float,
            fit_size: float):
    """Position + orbit distance, the stage's fitCameraToStage verbatim."""
    az = math.radians(az_deg)
    el = min(max(math.radians(el_deg), _EL_CLAMP[0]), _EL_CLAMP[1])
    fit_denom = 2 * math.atan(math.pi * _FOV_DEG / 360.0)
    fit_height = max(fit_size, _PLATE_MM) / fit_denom
    fit_width = (max(fit_size, 1.0) / fit_denom) / max(0.2, aspect)
    plate_dist = 1.3 * max(fit_height, fit_width)
    d_min = max(0.5, fit_radius * 1.12)
    d_max = plate_dist * 3.0
    dist = max(d_min, min(d_max, fit_radius * _STILL_DIST_FACTOR))
    eye = _np.array([
        dist * math.cos(el) * math.sin(az),
        dist * math.sin(el),
        dist * math.cos(el) * math.cos(az),
    ])
    return eye, dist


def _view_projection(eye, w: int, h: int):
    """World → pixel mapping for a camera at *eye* looking at the origin."""
    fwd = -eye / _np.linalg.norm(eye)
    up = _np.array([0.0, 1.0, 0.0])
    right = _np.cross(fwd, up)
    nr = _np.linalg.norm(right)
    if nr < 1e-9:  # straight up/down: pick a stable right-hand basis
        right = _np.array([1.0, 0.0, 0.0])
        nr = 1.0
    right = right / nr
    cam_up = _np.cross(right, fwd)
    focal = (h / 2.0) / math.tan(math.radians(_FOV_DEG) / 2.0)

    def project(points):
        rel = points - eye
        x = rel @ right
        y = rel @ cam_up
        z = rel @ fwd  # depth along view, positive in front
        z = _np.maximum(z, 1e-6)
        px = w / 2.0 + focal * x / z
        py = h / 2.0 - focal * y / z
        return px, py, z

    return project, fwd


def _shade(albedo_lin, normals, view):
    """Per-pixel RGB in sRGB bytes: Lambert diffuse + GGX specular.

    The material is three's MeshPhysicalMaterial (roughness 0.4,
    metalness 0.05), so the specular is the real Cook-Torrance lobe --
    GGX distribution, Schlick Fresnel, Smith visibility (UE4 k=a/2
    approximation) -- not a Blinn stand-in.  A Blinn lobe could be
    fitted to match any ONE pose's tone; what it cannot fake is the
    VIEW-dependence that makes the photograph's steep poses read
    brighter than its low ones, and the wall/top contrast that makes
    carved text read at all.  Only the per-light output scales and the
    exposure are fitted; the BRDF is the material's own.
    """
    a = _ROUGHNESS * _ROUGHNESS
    a2 = a * a
    k_vis = a / 2.0
    f0 = 0.04 + _METALNESS * (float(albedo_lin.mean()) - 0.04)

    nv = _np.clip((normals * view).sum(axis=1), 1e-4, None)
    color = _np.full((len(normals), 3), _AMBIENT)
    for (direction, light_rgb, intensity), scale in zip(
        _LIGHTS, _LIGHT_SCALES, strict=True
    ):
        intensity = intensity * scale
        ldir = _np.asarray(direction, dtype=_np.float64)
        ldir = ldir / _np.linalg.norm(ldir)
        ndl = _np.clip(normals @ ldir, 0.0, None)
        half = ldir[None, :] + view
        half = half / _np.maximum(_np.linalg.norm(half, axis=1), 1e-12)[:, None]
        ndh = _np.clip((normals * half).sum(axis=1), 0.0, None)
        vdh = _np.clip((view * half).sum(axis=1), 0.0, None)

        d = a2 / _np.maximum(_np.pi * (ndh * ndh * (a2 - 1.0) + 1.0) ** 2, 1e-9)
        fres = f0 + (1.0 - f0) * (1.0 - vdh) ** 5
        vis = 1.0 / _np.maximum(
            4.0 * (ndl * (1 - k_vis) + k_vis) * (nv * (1 - k_vis) + k_vis), 1e-9
        )
        spec = d * fres * vis

        contrib = (ndl + spec * ndl)[:, None] * _np.asarray(light_rgb)
        color += intensity * contrib
    color = color * albedo_lin[None, :] * _EXPOSURE
    srgb = _linear_to_srgb(_aces(color))
    return _np.clip(srgb * 255.0 + 0.5, 0, 255).astype(_np.uint8)


def _plate_texture(footprint):
    """The print bed, the canvas port from mesh_viewer.html, plus the
    contact blob composited in texture space.

    *footprint* is ``(cx, cz, dx, dz)`` of the model in plate coordinates
    (mm, origin at plate centre), or ``None`` for no blob.
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    # Drawn 4x oversampled and Lanczos-reduced: the browser canvas draws
    # its minor lines at 0.6 px with sub-pixel AA coverage, which a 1 px
    # hard PIL line badly overstates (measured 3.5x the photograph's
    # grid-pixel count before this).  At 4x, 0.6 px becomes a drawable
    # 2-3 px, and the reduction hands back the canvas's soft coverage.
    ov = 4
    tex_px = int(_PLATE_MM * _PX_PER_MM)
    big = tex_px * ov
    img = Image.new("RGB", (big, big), _BG)
    base = Image.new("RGBA", (big, big), _PLATE_BASE)
    img.paste(Image.alpha_composite(
        Image.new("RGBA", (big, big), _BG + (255,)), base).convert("RGB"))
    draw = ImageDraw.Draw(img, "RGBA")

    cell_px = _CELL_MM * _PX_PER_MM * ov
    lines = int(_PLATE_MM // _CELL_MM)
    # JS Math.round half-rounds UP; Python's round() half-rounds to even,
    # which put the centre cross one whole cell from the stage's.
    centre = math.floor(lines / 2 + 0.5)
    for i in range(lines + 1):
        p = i * cell_px
        is_c = i == centre
        colr = _GRID_CENTRE if is_c else _GRID_MINOR
        wdt = (2 if is_c else 0.6) * ov
        draw.line([(p, 0), (p, big)], fill=colr, width=int(round(wdt)))
        draw.line([(0, p), (big, p)], fill=colr, width=int(round(wdt)))
    draw.rectangle([1 * ov, 1 * ov, big - 2 * ov, big - 2 * ov],
                   outline=_RIM, width=2 * ov)

    font_px = max(14, min(28, round(tex_px / 36))) * ov
    font = None
    for cand in (
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ):
        try:
            font = ImageFont.truetype(cand, font_px)
            break
        except OSError:
            continue
    if font is not None:
        margin = max(10 * ov, int(font_px * 0.7))
        text = "K I L N"  # letterSpacing: 3px, spelled out
        tw = draw.textlength(text, font=font)
        draw.text((big - margin - tw, big - margin - font_px),
                  text, fill=_STAMP, font=font)

    if footprint is not None:
        cx, cz, dx, dz = footprint
        radius_mm = max(dx, dz) * 0.55 + 4
        r_px = radius_mm * _PX_PER_MM * ov
        bx = (cx + _PLATE_MM / 2) * _PX_PER_MM * ov
        bz = (cz + _PLATE_MM / 2) * _PX_PER_MM * ov
        blob = Image.new("L", (big, big), 0)
        bd = ImageDraw.Draw(blob)
        # Radial gradient via concentric rings over the recorded stops.
        steps = 48
        for i in range(steps, 0, -1):
            t = i / steps
            if t <= 0.5:
                a = 0.5 + (0.26 - 0.5) * (t / 0.5)
            else:
                a = 0.26 * (1 - (t - 0.5) / 0.5)
            bd.ellipse([bx - r_px * t, bz - r_px * t,
                        bx + r_px * t, bz + r_px * t], fill=int(a * 255))
        blob = blob.filter(ImageFilter.GaussianBlur(radius=_PX_PER_MM * ov * 1.5))
        shadow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        shadow.putalpha(blob)
        img = Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")
    # Returned AT the oversampled resolution: the renderer samples it
    # bilinear per pixel, and reducing first just filters the texture
    # twice -- measured softer than the photograph's single-pass
    # GPU sampling.
    return img


def _rasterize(tris_px, tris_py, tris_invz, attrs, tex_np, albedo_lin, eye,
               w, h, pair_cap=120_000_000):
    """One z-buffered pass over a triangle soup.

    ``attrs`` carries, per triangle vertex, either a unit NORMAL scaled by
    1/z (model triangles — shaded per pixel after visibility, three's
    smooth shading) or a texture u/z, v/z pair padded with a leading -2
    sentinel (plate triangles — sampled from ``tex_np``).  Fully
    vectorized: every candidate (pixel, triangle) pair is laid out flat,
    barycentric-tested, then reduced per pixel by nearest depth.  Returns
    an (h, w, 3) uint8 buffer, or ``None`` when the pair budget says this
    frame is too heavy to paint honestly.
    """
    np = _np
    x0 = np.clip(np.floor(tris_px.min(axis=1)), 0, w - 1).astype(np.int64)
    x1 = np.clip(np.ceil(tris_px.max(axis=1)), 0, w - 1).astype(np.int64)
    y0 = np.clip(np.floor(tris_py.min(axis=1)), 0, h - 1).astype(np.int64)
    y1 = np.clip(np.ceil(tris_py.max(axis=1)), 0, h - 1).astype(np.int64)
    bw = x1 - x0 + 1
    bh = y1 - y0 + 1
    counts = bw * bh
    onscreen = (bw > 0) & (bh > 0) & (tris_invz > 0).all(axis=1)
    counts = np.where(onscreen, counts, 0)
    total = int(counts.sum())
    empty = np.zeros((h, w, 3), dtype=np.uint8)
    empty[:] = _BG
    if total == 0:
        return empty
    if total > pair_cap:
        logger.debug("stage paint: %d raster pairs exceeds the cap", total)
        return None

    tri_id = np.repeat(np.arange(len(counts)), counts)
    offsets = np.concatenate([[0], np.cumsum(counts)[:-1]])
    local = np.arange(total) - offsets[tri_id]
    px = x0[tri_id] + local % bw[tri_id]
    py = y0[tri_id] + local // bw[tri_id]
    cx = px + 0.5
    cy = py + 0.5

    ax, ay = tris_px[tri_id, 0], tris_py[tri_id, 0]
    bx, by = tris_px[tri_id, 1], tris_py[tri_id, 1]
    qx, qy = tris_px[tri_id, 2], tris_py[tri_id, 2]
    area = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax)
    w0 = (bx - cx) * (qy - cy) - (by - cy) * (qx - cx)
    w1 = (qx - cx) * (ay - cy) - (qy - cy) * (ax - cx)
    w2 = area - w0 - w1
    nz = np.abs(area) > 1e-12
    sgn = np.sign(area)
    inside = nz & (w0 * sgn >= 0) & (w1 * sgn >= 0) & (w2 * sgn >= 0)
    if not inside.any():
        return empty

    tri_id = tri_id[inside]
    px, py = px[inside], py[inside]
    b0 = w0[inside] / area[inside]
    b1 = w1[inside] / area[inside]
    b2 = w2[inside] / area[inside]
    invz = (b0 * tris_invz[tri_id, 0] + b1 * tris_invz[tri_id, 1]
            + b2 * tris_invz[tri_id, 2])

    pix = py * w + px
    order = np.lexsort((-invz, pix))
    pix_o = pix[order]
    first = np.ones(len(pix_o), dtype=bool)
    first[1:] = pix_o[1:] != pix_o[:-1]
    sel = order[first]

    t_sel = tri_id[sel]
    b0s, b1s, b2s, izs = b0[sel], b1[sel], b2[sel], invz[sel]
    # Perspective-correct attribute interpolation: attrs are pre-divided
    # by z per vertex, so (sum b_i * a_i/z_i) / (1/z) recovers a.
    a_interp = (b0s[:, None] * attrs[t_sel, 0]
                + b1s[:, None] * attrs[t_sel, 1]
                + b2s[:, None] * attrs[t_sel, 2]) / izs[:, None]

    rgb = np.empty((len(sel), 3), dtype=np.uint8)
    textured = attrs[t_sel, 0, 0] <= -1.5  # sentinel channel marks the plate
    if textured.any():
        u = np.clip(a_interp[textured, 1], 0.0, 1.0 - 1e-9)
        vv = np.clip(a_interp[textured, 2], 0.0, 1.0 - 1e-9)
        th, tw = tex_np.shape[:2]
        # Bilinear, matching the CanvasTexture's LinearFilter: nearest
        # sampling made grid lines shimmer at minification and staircase
        # at magnification, neither of which the photograph does.
        fx = u * tw - 0.5
        fy = vv * th - 0.5
        x0f = np.floor(fx)
        y0f = np.floor(fy)
        tx = (fx - x0f)[:, None]
        ty = (fy - y0f)[:, None]
        xa = np.clip(x0f.astype(np.int64), 0, tw - 1)
        xb = np.clip(xa + 1, 0, tw - 1)
        ya = np.clip(y0f.astype(np.int64), 0, th - 1)
        yb = np.clip(ya + 1, 0, th - 1)
        tex = tex_np.astype(np.float64)
        top = tex[ya, xa] * (1 - tx) + tex[ya, xb] * tx
        bot = tex[yb, xa] * (1 - tx) + tex[yb, xb] * tx
        rgb[textured] = np.clip(top * (1 - ty) + bot * ty + 0.5,
                                0, 255).astype(np.uint8)
    smooth = ~textured
    if smooth.any():
        n = a_interp[smooth, 0:3]
        ln = np.linalg.norm(n, axis=1)
        n = n / np.maximum(ln, 1e-12)[:, None]
        pos = a_interp[smooth, 3:6]
        view = eye[None, :] - pos
        view = view / np.maximum(np.linalg.norm(view, axis=1), 1e-12)[:, None]
        rgb[smooth] = _shade(albedo_lin, n, view)

    buf = empty.reshape(h * w, 3)
    buf[pix[sel]] = rgb
    return buf.reshape(h, w, 3)


def _clip_polygon_near(corners, uvs, eye, fwd, near):
    """Sutherland-Hodgman clip of a textured polygon against view depth.

    Returns ``(points, uvs)`` with everything at depth >= *near*, or
    ``None`` when the polygon is wholly behind the camera.  UVs
    interpolate linearly in WORLD space along each clipped edge, which
    is exact -- the cut point is a world-space lerp.
    """
    pts = [_np.asarray(c, dtype=_np.float64) for c in corners]
    # TRUE signed view depth -- project() clamps depth positive for the
    # divide, which would make behind-camera vertices look barely-in-front
    # and land the clip cuts nowhere near the near plane.
    depth = [float((q - eye) @ fwd) for q in pts]
    out_pts: list = []
    out_uvs: list = []
    n = len(pts)
    for i in range(n):
        a, b = i, (i + 1) % n
        da, db = depth[a] - near, depth[b] - near
        if da >= 0:
            out_pts.append(pts[a])
            out_uvs.append(uvs[a])
        if (da < 0) != (db < 0):
            t = da / (da - db)
            out_pts.append(pts[a] + (pts[b] - pts[a]) * t)
            out_uvs.append((
                uvs[a][0] + (uvs[b][0] - uvs[a][0]) * t,
                uvs[a][1] + (uvs[b][1] - uvs[a][1]) * t,
            ))
    if len(out_pts) < 3:
        return None
    return out_pts, out_uvs


def _paint_view(v, f, az_deg, el_deg, *, width, height, albedo_lin,
                floor_y, footprint, fit_radius, fit_size, plate_tex_np):
    """One still at full working resolution.  PIL image, or ``None``."""
    from PIL import Image

    np = _np
    eye, _dist = _camera(az_deg, el_deg, width / height, fit_radius, fit_size)
    project, fwd = _view_projection(eye, width, height)

    # Model triangles: backface-culled on FACE normals (the visibility
    # question), lit on interpolated VERTEX normals (the look question).
    tri = v[f]
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    fn = np.cross(e1, e2)
    ln = np.linalg.norm(fn, axis=1)
    keep = ln > 1e-12
    f2 = f[keep]
    fn = fn[keep] / ln[keep][:, None]
    centroids = tri[keep].mean(axis=1)
    facing = ((eye[None, :] - centroids) * fn).sum(axis=1) > 0
    f2 = f2[facing]

    all_px, all_py, all_iz, all_at = [], [], [], []

    if len(f2):
        tri = v[f2]
        # The payload ships NO normals on purpose ("the viewer flat-shades
        # via derivative normals, matching the faceted look of Kiln's
        # OpenSCAD previews" -- mesh_payload).  Flat is PARITY here, not a
        # shortcut: the face normal rides all three corners, so the
        # interpolator emits it constant across the face.
        tn = _np.repeat(fn[facing][:, None, :], 3, axis=1)
        px, py, pz = project(tri.reshape(-1, 3))
        iz = (1.0 / pz).reshape(-1, 3)
        all_px.append(px.reshape(-1, 3))
        all_py.append(py.reshape(-1, 3))
        all_iz.append(iz)
        # channels 0-2: normal/z; channels 3-5: position/z (for the
        # per-pixel view vector the specular needs)
        all_at.append(np.concatenate(
            [tn * iz[:, :, None], tri * iz[:, :, None]], axis=2))

    plate_y = floor_y - 0.2
    if eye[1] > plate_y and plate_tex_np is not None:
        half = _PLATE_MM / 2.0
        corners = [
            (-half, plate_y, -half), (half, plate_y, -half),
            (half, plate_y, half), (-half, plate_y, half),
        ]
        # Orientation pinned by the marker experiment in
        # test_stage_paint (a cube at a known world position must land
        # in the same screen quadrant as the photograph's): the canvas
        # x-axis runs along world -x on the rotated plane.
        uvs4 = [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        # The plate straddles the near plane whenever the part -- and so
        # the camera orbit -- is small (a 30 mm tag puts the camera
        # ~60 mm out; the plate reaches 128 mm PAST it).  The photograph
        # clips per-pixel on the GPU at near = dist/100; clip the
        # polygon the same place, then fan-triangulate what survives.
        near = _dist / 100.0
        poly = _clip_polygon_near(corners, uvs4, eye, fwd, near)
        if poly is not None:
            pts, uvs = poly
            pxc, pyc, pzc = project(np.asarray(pts))
            izc = 1.0 / pzc
            for k in range(1, len(pts) - 1):
                a, b, c = 0, k, k + 1
                all_px.append(np.array([[pxc[a], pxc[b], pxc[c]]]))
                all_py.append(np.array([[pyc[a], pyc[b], pyc[c]]]))
                all_iz.append(np.array([[izc[a], izc[b], izc[c]]]))
                # sentinel -2 in channel 0; u/z, v/z ride channels 1-2
                all_at.append(np.array([[
                    [-2.0, uvs[a][0] * izc[a], uvs[a][1] * izc[a], 0, 0, 0],
                    [-2.0, uvs[b][0] * izc[b], uvs[b][1] * izc[b], 0, 0, 0],
                    [-2.0, uvs[c][0] * izc[c], uvs[c][1] * izc[c], 0, 0, 0],
                ]]))

    if not all_px:
        return Image.new("RGB", (width, height), _BG)

    buf = _rasterize(
        np.vstack(all_px), np.vstack(all_py), np.vstack(all_iz),
        np.vstack(all_at), plate_tex_np, albedo_lin, eye, width, height,
    )
    if buf is None:
        return None
    return Image.fromarray(buf, "RGB")


_HEX_COLOR = None  # shared with stage_still, resolved lazily


def try_paint_stage_views(
    file_path: str,
    selected: list[tuple[str, str]],
    rotations: dict[str, tuple[float, float, float]],
    *,
    output_dir: str,
    width: int,
    height: int,
    color: str | None = None,
) -> list[dict] | None:
    """Paint every requested view in the stage look, or ``None``.

    The contract is :func:`kiln.stage_still.try_render_stage_views`'s,
    verbatim: ``None`` — never a partial list, never an exception — means
    "run the next backend"; the caller's angle machinery rides through
    unchanged; a non-hex *color* declines rather than guessing.
    """
    try:
        if os.environ.get(_OPT_OUT_ENV, "").strip():
            return None
        from kiln.stage_still import _HEX_COLOR as hex_re
        from kiln.stage_still import _openscad_rotation_to_orbit

        albedo_hex = _MODEL_COLOR
        if color:
            if not hex_re.match(color.strip()):
                logger.debug("stage paint: colour %r is not hex — declining", color)
                return None
            albedo_hex = color.strip()

        if _deps() is None:
            return None
        loaded = _load_viewer_frame_mesh(file_path)
        if loaded is None:
            return None
        v, f = loaded

        c, radius, lo, hi = _bounding_sphere(v)
        v = v - c  # centre the bounding sphere at the orbit target
        lo, hi = lo - c, hi - c
        floor_y = float(lo[1])
        fit_size = float(max(hi - lo))
        footprint = (
            float((lo[0] + hi[0]) / 2), float((lo[2] + hi[2]) / 2),
            float(hi[0] - lo[0]), float(hi[2] - lo[2]),
        )

        h = albedo_hex.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        albedo_lin = _srgb_to_linear(
            _np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)]) / 255.0
        )

        from kiln.preview_render import downscale_png, effective_supersample

        ss = effective_supersample()
        stem = Path(file_path).stem
        os.makedirs(output_dir, mode=0o700, exist_ok=True)

        plate_tex_np = _np.asarray(_plate_texture(footprint), dtype=_np.uint8)

        views: list[dict] = []
        for label, description in selected:
            rx, _ry, rz = rotations[label]
            az, el = _openscad_rotation_to_orbit(rx, rz)
            # One supersample step past the shared knob, internally: the
            # photograph gets GPU MSAA on top of the same 2x-and-downscale
            # pipeline, and without this the painted edges measured
            # visibly harsher (mean edge gradient 69 vs the photograph's
            # 54).  The knob still governs the OUTPUT contract; this is
            # the renderer's own anti-aliasing, like the browser's MSAA
            # is the browser's.
            ss_int = min(ss + 1, 4)
            # The 56 CSS-px footer is a fraction of the BROWSER's page at
            # the user's supersample; keep that fraction at the internal
            # resolution so the two backends stay geometrically
            # interchangeable at any knob setting.
            full_h = height * ss_int
            strip = round(_FOOTER_PX * ss_int / ss)
            canvas_h = full_h - strip
            if canvas_h < 32:  # degenerate request: skip the letterbox
                canvas_h = full_h
            img = _paint_view(
                v, f, az, el,
                width=width * ss_int, height=canvas_h,
                albedo_lin=albedo_lin, floor_y=floor_y,
                footprint=footprint, fit_radius=radius, fit_size=fit_size,
                plate_tex_np=plate_tex_np,
            )
            if img is None:  # raster budget said no — all or nothing
                return None
            if canvas_h != full_h:
                from PIL import Image as _Image

                page = _Image.new("RGB", (width * ss_int, full_h), _BG)
                page.paste(img, (0, 0))
                img = page
            out = os.path.join(output_dir, f"{stem}_{label}.png")
            img.save(out)
            if ss_int > 1:
                downscale_png(out, width, height)
            views.append({"angle": label, "description": description, "path": out})
        return views
    except Exception:  # noqa: BLE001 — a paint failure must never break a preview
        logger.debug("stage paint failed — falling through", exc_info=True)
        return None
