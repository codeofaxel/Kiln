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
four-light rig — with numpy and Pillow, both of which are core Kiln
dependencies.  Pillow became one FOR this module: it shipped believing
that was already true, and it was not.  On 1.4.1 a default
``pip install kiln3d`` carried no Pillow, so ``_deps()`` returned None,
this painter declined, and the preview fell back to the OpenSCAD look
with nothing said — the module was inert for every user who installed
the documented way.  It is not a new look; every constant below is transcribed
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

The geometry is the stage's own payload (``kiln.local_stage.
_payload_for_mesh``), a painted part's colours included, so a painted
part on a machine with no usable browser keeps the stage look.  Until
2026-09-22 this module read files with trimesh, saw no colours, and was
skipped for every painted part; those previews fell to the grey per-face
renderer, which no person is meant to be shown.

Everything here is best-effort and silent, exactly like the photograph
path: any miss returns ``None`` and the caller falls through.  The same
``KILN_NO_STAGE_STILLS=1`` opt-out disables both stage-look backends —
it means "give me the OpenSCAD look", not "avoid browsers".

WHAT IS APPROXIMATED, HONESTLY
------------------------------
No bloom pass.  The stage's composer runs an UnrealBloomPass and this
does not: bloom is a screen-space blur of the blown highlights added
back over the frame, not a per-pixel shading term, so it shows up here
as the brightest faces reading a little darker than the photograph's
(measured 2026-09-22: agreement within a few tone levels up to ~180/255,
falling behind above it).  The calibration masks the halo out rather
than paying for it with the constants everything else depends on.

The light rig's OUTPUT levels are fitted rather than transcribed: the
_LIGHTS intensities and the environment's intensity are three.js
-internal units that do not survive three's physically-scaled pipeline
into pixel values, so _LIGHT_SCALES / _AMBIENT / _ENV_SCALE / _EXPOSURE
are measured off real photographs by
``kiln/scripts/calibrate_stage_paint.py`` -- the sphere-probe method
documented there; re-run it whenever the stage document's rig changes.
The environment gradient's SHAPE is transcribed like everything else,
and its two convolutions (diffuse and specular) are three's own.  The
BRDF itself is not approximated: real GGX
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
import time
from pathlib import Path
from typing import NamedTuple

from kiln import _fonts

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
_AMBIENT = 0.110  # fitted: the transcribed 0.35 is a three-internal unit
_LIGHTS = (
    # (direction xyz, color rgb 0..1, intensity)
    ((10.0, 20.0, 10.0), (1.0, 0xF7 / 0xFF, 0xEE / 0xFF), 1.0),
    ((-15.0, 8.0, -10.0), (0xD8 / 0xFF, 0xE1 / 0xFF, 1.0), 0.5),
    ((16.0, 6.0, 1.5), (1.0, 1.0, 1.0), 0.75),
    ((-16.0, 6.0, 1.5), (1.0, 1.0, 1.0), 0.5),
)

# Environment: `scene.environment = buildEnvMap(renderer)` — "a
# studio-softbox vertical gradient run through PMREM so the physical
# material has something believable to reflect".  A 256x128 canvas
# filled by `createLinearGradient(0, h, 0, 0)` with the stops below,
# uploaded as an equirectangular sRGB `CanvasTexture`.
#
# Stop `s` → direction: a CanvasTexture flips Y (`Texture.flipY` is
# true), so stop 0 (drawn at the canvas BOTTOM) lands at texture v = 0
# and stop 1 at v = 1; three's `equirectUv` sets
# `v = asin(dir.y)/PI + 0.5`, so `dir.y = -cos(PI * s)`.  s = 0 is
# straight DOWN, s = 1 straight UP.  Which is why this term exists: the
# lower hemisphere is a lit slate, not black, and a downward-facing face
# — the one the four lights all miss — is lit almost entirely by it.
_ENV_STOPS = (
    (0.00, (88, 104, 120)),   # straight down
    (0.45, (26, 34, 45)),     # just under the horizon
    (0.78, (150, 152, 158)),  # ~50 deg up
    (1.00, (255, 255, 255)),  # straight up
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
#: tones both match the photograph.  They dropped sharply on 2026-09-22
#: (from 0.601 / 1.970 / 0.234) when the environment term arrived: the
#: old rig had been standing in for the environment's light, the rim
#: most of all, and with the real term in place it no longer has to.
#: Carved-text contrast came out CLOSER to the photograph for it
#: (wall high-pass 10.4 against the photograph's 10.4, was 11.3).
_LIGHT_SCALES = (0.170, 0.063, 0.271, 0.271)

#: Environment-map intensity, fitted the same way.  three's
#: `MeshPhysicalMaterial` defaults `envMapIntensity` to 1 and its
#: indirect diffuse is `envColor * albedo` with NO 1/PI, while the
#: painter's direct terms carry three's light-unit conversion inside
#: _LIGHT_SCALES and _EXPOSURE -- so the honest expectation here is
#: 1 / _EXPOSURE ~= 1.4, not 1.0, and landing near it is the
#: transcription checking itself.
_ENV_SCALE = 1.502


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:  # noqa: F821
    return _np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:  # noqa: F821
    c = _np.clip(c, 0.0, 1.0)
    return _np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


#: ``(diffuse, specular)`` lookup tables, built once on first paint.
_ENV_TABLES = None
_ENV_TABLE_N = 257  # the curves are smooth; 257 knots resolve them to <0.1/255


def _env_gradient(s):
    """The canvas gradient's LINEAR RGB at stop fractions *s*.

    A CSS gradient interpolates in sRGB byte space and the texture is
    uploaded as ``SRGBColorSpace``, so the stops are mixed first and
    decoded second — doing it the other way round lightens the whole
    lower hemisphere by several tone levels.
    """
    stops = _np.asarray([st for st, _ in _ENV_STOPS])
    cols = _np.asarray([c for _, c in _ENV_STOPS], dtype=_np.float64) / 255.0
    mixed = _np.stack(
        [_np.interp(s, stops, cols[:, ch]) for ch in range(3)], axis=-1
    )
    return _srgb_to_linear(mixed)


def _env_radiance_at(y):
    """The gradient's linear RGB for directions with this y.

    ``y = -cos(PI * s)`` inverted: the stop a direction reads from.
    """
    return _env_gradient(_np.arccos(_np.clip(-y, -1.0, 1.0)) / math.pi)


def _build_env_diffuse():
    """Cosine-convolve the gradient — three's roughness-1 PMREM, in 1-D.

    ``getIBLIrradiance`` samples the PMREM chain's roughest level
    (``textureCubeUV(envMap, N, 1.0)``) and returns ``PI * envColor``,
    which the Lambert BRDF's 1/PI then cancels — so the indirect diffuse
    is ``envColor * diffuseColor``, and ``envColor`` is the cosine-weighted
    MEAN RADIANCE about the normal.  That is what this returns.

    The gradient varies only with elevation, so the convolution is
    azimuthally symmetric: the result depends on the normal's y alone,
    and a 1-D table serves every pixel.  The azimuth integral is closed
    form — for ``max(a + b cos(phi), 0)`` with ``b >= 0`` it is ``2*pi*a``
    when ``a >= b``, zero when ``a <= -b``, and ``2*(a*p + b*sin(p))``
    with ``p = arccos(-a/b)`` in between — so only the polar integral is
    quadrature, and 2048 knots put it well past PNG resolution.
    """
    ny = _np.linspace(-1.0, 1.0, _ENV_TABLE_N)
    m = 2048
    theta = (_np.arange(m) + 0.5) * (math.pi / m)  # polar angle from +Y
    ct, st = _np.cos(theta), _np.sin(theta)
    radiance = _env_radiance_at(ct)

    a = ny[:, None] * ct[None, :]
    b = _np.sqrt(_np.maximum(1.0 - ny * ny, 0.0))[:, None] * st[None, :]
    safe = _np.maximum(b, 1e-12)
    p = _np.arccos(_np.clip(-a / safe, -1.0, 1.0))
    phi = 2.0 * (a * p + b * _np.sin(p))
    phi = _np.where(a >= b, 2.0 * math.pi * a, phi)
    phi = _np.where(a <= -b, 0.0, phi)

    weight = phi * st[None, :] * (math.pi / m)
    irradiance = weight @ radiance  # (N, 3) — the full irradiance E
    return irradiance / math.pi  # three's envColor = E / PI


def _build_env_specular():
    """The gradient prefiltered for ``getIBLRadiance`` at this roughness.

    The environment lights the part through the specular lobe as well as
    the diffuse one, and unlike the diffuse term that one is VIEW
    -dependent: it reads the gradient along the REFLECTION vector.  It is
    what makes a near-horizontal wall read brighter from below than from
    above, and a fit with the diffuse half alone left near-horizontal
    faces seen from below 32 tone levels under the photograph.

    Kernel: the split-sum prefilter three's PMREM approximates — GGX half
    -vectors about the reflection direction (``N = V = R``), each mapping
    to a sample direction at twice its angle and weighted by ``N·L``.
    The blur is close, not identical: three's PMREM runs a Gaussian
    approximation of this lobe over a mip chain, and the whole level is
    fitted by ``_ENV_SCALE`` anyway.  Azimuth needs quadrature here (a
    GGX lobe's has no closed form), but the geometry stays 1-D: a sample
    at polar offset ``psi`` and azimuth ``al`` from a direction whose own
    polar angle is ``beta`` has ``y = cos(psi)cos(beta) -
    sin(psi)cos(al)sin(beta)``.
    """
    alpha = _ROUGHNESS * _ROUGHNESS
    a2 = alpha * alpha
    # Half-vector angles: beyond PI/4 the sample direction falls below the
    # horizon (N·L <= 0) and the split-sum drops it.
    nh, na = 192, 128
    th = (_np.arange(nh) + 0.5) * (0.25 * math.pi / nh)
    psi = 2.0 * th
    cth = _np.cos(th)
    d = a2 / (math.pi * (cth * cth * (a2 - 1.0) + 1.0) ** 2)
    w = d * cth * _np.sin(th) * _np.cos(psi)  # GGX measure x N·L
    al = (_np.arange(na) + 0.5) * (2.0 * math.pi / na)

    beta = _np.arccos(_np.clip(_np.linspace(-1.0, 1.0, _ENV_TABLE_N), -1.0, 1.0))
    out = _np.empty((_ENV_TABLE_N, 3))
    cpsi, spsi = _np.cos(psi), _np.sin(psi)
    cal = _np.cos(al)
    for i, b in enumerate(beta):
        y = cpsi[:, None] * math.cos(b) - spsi[:, None] * cal[None, :] * math.sin(b)
        rad = _env_radiance_at(y)  # (nh, na, 3)
        out[i] = (w @ rad.sum(axis=1)) / (w.sum() * na)
    return out


def _env_tables():
    """``(diffuse, specular)`` tables over the direction's y, built once."""
    global _ENV_TABLES
    if _ENV_TABLES is None:
        _ENV_TABLES = (_build_env_diffuse(), _build_env_specular())
    return _ENV_TABLES


def _env_lookup(table, y):
    """Linear RGB from a direction-y table, linearly interpolated."""
    idx = _np.clip((y + 1.0) * 0.5 * (_ENV_TABLE_N - 1), 0, _ENV_TABLE_N - 1.001)
    lo = idx.astype(_np.int64)
    t = (idx - lo)[:, None]
    return table[lo] * (1.0 - t) + table[lo + 1] * t


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
    """``(vertices, faces, colours)`` in the stage's y-up frame, or ``None``.

    Read from the stage's own payload door (:func:`kiln.local_stage.
    _payload_for_mesh`), the one the live panel and the photograph draw
    from, so the painter paints what the stage shows.  It used to call
    ``trimesh.load`` itself, which cost it two things: a painted part's
    colours, which trimesh never reads from a 3MF, so every painted part
    skipped this backend for the grey renderer; and every 3MF on a plain
    install, where trimesh's 3MF loader lacks the libraries it needs and
    the payload's own reader does not.

    ``colours`` is the payload's RGBA per vertex, or ``None`` for a part
    that carries none.  Positions arrive already rotated into the viewer
    frame, (x, y, z)_mesh → (x, z, -y)_viewer, so the light rig and orbit
    mapping stay verbatim.  A payload the door had to decimate or leave
    out declines, the same as a mesh past the face cap always has.
    """
    import base64

    from kiln.local_stage import _payload_for_mesh
    from kiln.stage_still import _STILL_MAX_BYTES

    try:
        payload = _payload_for_mesh(
            file_path, max_triangles=_MAX_FACES, max_bytes=_STILL_MAX_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 — any unreadable source → decline
        logger.debug("stage paint: cannot read %s: %s", file_path, exc)
        return None
    if not payload or payload.get("downgraded") or payload.get("decimated_from"):
        logger.debug("stage paint: %s is past the %d-face cap", file_path, _MAX_FACES)
        return None
    v = _np.frombuffer(base64.b64decode(payload["positions"]), dtype="<f4")
    f = _np.frombuffer(base64.b64decode(payload["indices"]), dtype="<u4")
    v = v.astype(_np.float64).reshape(-1, 3)
    f = f.astype(_np.int64).reshape(-1, 3)
    if len(f) == 0:
        return None
    colours = None
    if payload.get("vertex_colors"):
        rgba = _np.frombuffer(base64.b64decode(payload["vertex_colors"]), dtype=_np.uint8)
        if len(rgba) == len(v) * 4:
            colours = rgba.reshape(-1, 4)
    return v, f, colours


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
    """Per-pixel RGB in sRGB bytes: environment + Lambert + GGX specular.

    The stage lights the part from ``scene.environment`` as well as from
    the four directionals, and that term is the one a face pointing
    DOWN lives on: the four lights all arrive from above, so a downward
    face falls to the indirect light alone.  A flat ambient constant put
    a bottom view 40 tone levels under the photograph; the gradient's own
    convolutions (:func:`_env_tables`) put it back, because they VARY —
    the diffuse half with the normal, the specular half with the
    REFLECTION, and the stage's softbox is a lit slate below, near-black
    at the horizon and white overhead.

    The material is three's MeshPhysicalMaterial (roughness 0.4,
    metalness 0.05), so the specular is the real Cook-Torrance lobe --
    GGX distribution, Schlick Fresnel, Smith visibility (UE4 k=a/2
    approximation) -- not a Blinn stand-in.  A Blinn lobe could be
    fitted to match any ONE pose's tone; what it cannot fake is the
    VIEW-dependence that makes the photograph's steep poses read
    brighter than its low ones, and the wall/top contrast that makes
    carved text read at all.  Only the per-light output scales and the
    exposure are fitted; the BRDF is the material's own.

    *albedo_lin* is one linear RGB for the whole part, or one per pixel
    ``(N, 3)`` for a part carrying its own colours; the Fresnel base
    follows it per pixel, as the material's does.
    """
    a = _ROUGHNESS * _ROUGHNESS
    a2 = a * a
    k_vis = a / 2.0
    if albedo_lin.ndim == 2:
        f0 = 0.04 + _METALNESS * (albedo_lin.mean(axis=1) - 0.04)
    else:
        f0 = 0.04 + _METALNESS * (float(albedo_lin.mean()) - 0.04)

    nv = _np.clip((normals * view).sum(axis=1), 1e-4, None)

    # The environment, three's RE_IndirectSpecular_Physical (which owns
    # the indirect DIFFUSE too).  DFGApprox is the split-sum term, the
    # multi-scatter compensation follows, and the diffuse half is
    # attenuated by what the specular half took -- all transcribed.
    diff_tbl, spec_tbl = _env_tables()
    env_d = _ENV_SCALE * _env_lookup(diff_tbl, normals[:, 1])
    refl = 2.0 * nv[:, None] * normals - view  # reflect(-view, normal)
    refl = refl + (normals - refl) * a  # mix(reflectVec, normal, roughness^2)
    refl = refl / _np.maximum(_np.linalg.norm(refl, axis=1), 1e-12)[:, None]
    env_s = _ENV_SCALE * _env_lookup(spec_tbl, refl[:, 1])
    dnv = _np.clip(nv, 0.0, 1.0)
    r_x = -_ROUGHNESS + 1.0
    a004 = _np.minimum(r_x * r_x, _np.exp2(-9.28 * dnv)) * r_x + (
        -0.0275 * _ROUGHNESS + 0.0425
    )
    fab_x = -1.04 * a004 + (-0.572 * _ROUGHNESS + 1.04)
    fab_y = 1.04 * a004 + (0.022 * _ROUGHNESS - 0.04)
    fss_ess = f0 * fab_x + fab_y  # specularF90 is 1 for this material
    ems = 1.0 - (fab_x + fab_y)
    favg = f0 + (1.0 - f0) * 0.047619
    multi = fss_ess * favg / _np.maximum(1.0 - ems * favg, 1e-9) * ems
    total_scatter = fss_ess + multi

    color = (
        _AMBIENT
        + env_d * ((1.0 - total_scatter) * (1.0 - _METALNESS))[:, None]
    )
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
    color = color * (albedo_lin if albedo_lin.ndim == 2 else albedo_lin[None, :])
    # The environment's specular half is NOT tinted by the part's colour,
    # exactly as three adds it after the diffuse -- a dark logo on a
    # painted part keeps its highlight instead of swallowing it.
    color += env_s * fss_ess[:, None] + env_d * multi[:, None]
    srgb = _linear_to_srgb(_aces(color * _EXPOSURE))
    return _np.clip(srgb * 255.0 + 0.5, 0, 255).astype(_np.uint8)


def _plate_texture(footprint):
    """The print bed, the canvas port from mesh_viewer.html, plus the
    contact blob composited in texture space.

    *footprint* is ``(cx, cz, dx, dz)`` of the model in plate coordinates
    (mm, origin at plate centre), or ``None`` for no blob.
    """
    from PIL import Image, ImageDraw, ImageFilter

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
    # find_font, not load_font: this plate is a transcription of the one
    # the photograph path shoots, pinned by the calibration tests below.
    # A mark set in Pillow's substitute face would be legible and wrong --
    # drift from the thing being matched.  No real face means no stamp.
    font = _fonts.find_font(font_px, bold=True)
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


#: Upper bound on candidate (pixel, triangle) pairs materialized at once
#: by :func:`_rasterize`.  ~15 float64 working arrays ride each pair, so a
#: slice of 1M pairs is a ~200 MB transient; the per-pixel winner buffers
#: add 5 x (w*h) float64/int64 on top.  Small enough that two concurrent
#: renders fit a 2 GB host beside a running API; large enough that slice
#: overhead stays noise.  Numpy expression temporaries ride each slice at
#: roughly 2x the named arrays, so the working set is ~150 MB here —
#: measured, not estimated (1M-pair slices peaked ~880 MB in _rasterize).
_PAIR_SLICE = 400_000

#: Candidate (pixel, triangle) pairs one view may cost.  With slicing
#: this is a TIME bound, not a memory one.  It is a property of the VIEW
#: — projected triangle bounding boxes at this frame size — so a set can
#: contain views on both sides of it, and since the set is
#: all-or-nothing, one view over the cap dooms every view under it.
#: Hence :func:`_pair_count`, which prices a view in arithmetic alone,
#: and the pre-pass in :func:`try_paint_stage_views` that runs it over
#: the whole set first.  Measured 2026-09-06: painting four 1600x1200
#: views and then meeting the cap on the fifth spent ~30 s to arrive at
#: the same "no" the pre-pass reaches before the first pixel.
_PAIR_CAP = 120_000_000


class _ViewCost(NamedTuple):
    """What one view costs, and the box arithmetic that priced it.

    ``total`` is the whole answer for the pre-pass; the rasterizer also
    keeps the per-triangle boxes, so the pixels it walks come from the
    same arithmetic that quoted them.
    """

    x0: object
    y0: object
    bw: object
    counts: object
    total: int


def _pair_count(tris_px, tris_py, tris_invz, w, h) -> _ViewCost:
    """Price one view without painting it.

    The rasterizer's own first step, lifted out so the set's pre-pass can
    ask what a view costs without paying for it.  One helper, so the two
    can never disagree about what a view is worth.
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
    return _ViewCost(x0, y0, bw, counts, int(counts.sum()))


def _rasterize(tris_px, tris_py, tris_invz, attrs, tex_np, albedo_lin, eye,
               w, h, pair_cap=None):
    """One z-buffered pass over a triangle soup.

    ``attrs`` carries, per triangle vertex, either a unit NORMAL scaled by
    1/z (model triangles — shaded per pixel after visibility, three's
    smooth shading) or a texture u/z, v/z pair padded with a leading -2
    sentinel (plate triangles — sampled from ``tex_np``).  A part that
    carries its own colours adds a linear RGB/z in channels 6-8, which
    replaces ``albedo_lin`` pixel by pixel.  Vectorized in
    BOUNDED SLICES: candidate (pixel, triangle) pairs are laid out flat at
    most ``_PAIR_SLICE`` at a time and reduced into a persistent per-pixel
    nearest-depth winner, so peak memory tracks the slice and the
    framebuffer — never the scene.  (The one-shot layout this replaces
    materialized ~15 float64 arrays over EVERY pair: >5 GB for an ordinary
    supersampled 800x600 still, which OOM-killed a 2 GB host that asked
    for one thumbnail.  Same formulas, same nearest-depth rule, same tie
    order — the earliest pair among depth-equals wins, because a slice's
    stable lexsort keeps first occurrence and later slices replace only on
    STRICTLY nearer depth — so the output is bit-identical.)  Returns an
    (h, w, 3) uint8 buffer, or ``None`` when the pair budget says this
    frame is too heavy to paint honestly (with slicing that is a TIME
    bound; memory no longer scales with the total).
    """
    np = _np
    if pair_cap is None:
        pair_cap = _PAIR_CAP
    x0, y0, bw, counts, total = _pair_count(tris_px, tris_py, tris_invz, w, h)
    empty = np.zeros((h, w, 3), dtype=np.uint8)
    empty[:] = _BG
    if total == 0:
        return empty
    if total > pair_cap:
        logger.debug("stage paint: %d raster pairs exceeds the cap", total)
        return None

    cum = np.cumsum(counts)
    offsets = cum - counts

    # Per-pixel winner state, float64 throughout so the shading below sees
    # exactly the numbers the one-shot layout produced.
    best_invz = np.zeros(h * w, dtype=np.float64)
    best_b0 = np.empty(h * w, dtype=np.float64)
    best_b1 = np.empty(h * w, dtype=np.float64)
    best_b2 = np.empty(h * w, dtype=np.float64)
    best_tri = np.zeros(h * w, dtype=np.int64)

    for start in range(0, total, _PAIR_SLICE):
        pair = np.arange(start, min(start + _PAIR_SLICE, total))
        tri_id = np.searchsorted(cum, pair, side="right")
        local = pair - offsets[tri_id]
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
            continue

        tri_id = tri_id[inside]
        px, py = px[inside], py[inside]
        b0 = w0[inside] / area[inside]
        b1 = w1[inside] / area[inside]
        b2 = w2[inside] / area[inside]
        invz = (b0 * tris_invz[tri_id, 0] + b1 * tris_invz[tri_id, 1]
                + b2 * tris_invz[tri_id, 2])

        # Nearest-depth per pixel within the slice (stable lexsort keeps
        # the earliest among equals), then merge into the running winners.
        pix = py * w + px
        order = np.lexsort((-invz, pix))
        pix_o = pix[order]
        first = np.ones(len(pix_o), dtype=bool)
        first[1:] = pix_o[1:] != pix_o[:-1]
        sel = order[first]

        p_sel = pix[sel]
        upd = invz[sel] > best_invz[p_sel]
        p_upd = p_sel[upd]
        s_upd = sel[upd]
        best_invz[p_upd] = invz[s_upd]
        best_b0[p_upd] = b0[s_upd]
        best_b1[p_upd] = b1[s_upd]
        best_b2[p_upd] = b2[s_upd]
        best_tri[p_upd] = tri_id[s_upd]

    hit_all = np.nonzero(best_invz > 0.0)[0]
    if len(hit_all) == 0:
        return empty

    # Interpolation + shading are pure per-pixel arithmetic, so they get
    # the same slice treatment as the pair sweep — a dozen working arrays
    # over EVERY hit pixel at once was the other gigabyte.
    buf = empty.reshape(h * w, 3)
    for hstart in range(0, len(hit_all), _PAIR_SLICE):
        hit = hit_all[hstart:hstart + _PAIR_SLICE]
        t_sel = best_tri[hit]
        b0s, b1s, b2s, izs = (best_b0[hit], best_b1[hit],
                              best_b2[hit], best_invz[hit])
        # Perspective-correct attribute interpolation: attrs are pre-divided
        # by z per vertex, so (sum b_i * a_i/z_i) / (1/z) recovers a.
        a_interp = (b0s[:, None] * attrs[t_sel, 0]
                    + b1s[:, None] * attrs[t_sel, 1]
                    + b2s[:, None] * attrs[t_sel, 2]) / izs[:, None]

        rgb = np.empty((len(hit), 3), dtype=np.uint8)
        textured = attrs[t_sel, 0, 0] <= -1.5  # sentinel marks the plate
        if textured.any():
            u = np.clip(a_interp[textured, 1], 0.0, 1.0 - 1e-9)
            vv = np.clip(a_interp[textured, 2], 0.0, 1.0 - 1e-9)
            th, tw = tex_np.shape[:2]
            # Bilinear, matching the CanvasTexture's LinearFilter: nearest
            # sampling made grid lines shimmer at minification and
            # staircase at magnification, neither of which the photograph
            # does.
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
            # Gather uint8 texels FIRST, convert the gathers — identical
            # arithmetic to converting the whole texture up front, without
            # holding a float64 copy of the full plate (hundreds of MB at
            # the oversampled plate resolution).
            top = (tex_np[ya, xa].astype(np.float64) * (1 - tx)
                   + tex_np[ya, xb].astype(np.float64) * tx)
            bot = (tex_np[yb, xa].astype(np.float64) * (1 - tx)
                   + tex_np[yb, xb].astype(np.float64) * tx)
            rgb[textured] = np.clip(top * (1 - ty) + bot * ty + 0.5,
                                    0, 255).astype(np.uint8)
        smooth = ~textured
        if smooth.any():
            n = a_interp[smooth, 0:3]
            ln = np.linalg.norm(n, axis=1)
            n = n / np.maximum(ln, 1e-12)[:, None]
            pos = a_interp[smooth, 3:6]
            view = eye[None, :] - pos
            view = view / np.maximum(
                np.linalg.norm(view, axis=1), 1e-12)[:, None]
            albedo = a_interp[smooth, 6:9] if attrs.shape[2] > 6 else albedo_lin
            rgb[smooth] = _shade(albedo, n, view)

        buf[hit] = rgb
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
                floor_y, footprint, fit_radius, fit_size, plate_tex_np,
                cost_only=False, vertex_albedo=None):
    """One still at full working resolution.  PIL image, or ``None``.

    ``cost_only`` stops after the geometry — every projection and clip
    the real pass makes, none of the rasterizing — and returns the view's
    pair count as an int.  That is what the set's pre-pass asks with, so
    the price it is quoted is the price the rasterizer will charge.

    ``vertex_albedo`` is the part's own linear RGB per vertex, or ``None``
    to paint the whole part in ``albedo_lin``.
    """
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
        # per-pixel view vector the specular needs); 6-8: the part's own
        # colour/z, when it carries one
        channels = [tn * iz[:, :, None], tri * iz[:, :, None]]
        if vertex_albedo is not None:
            channels.append(vertex_albedo[f2] * iz[:, :, None])
        all_at.append(np.concatenate(channels, axis=2))

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
            # The plate's rows are as wide as the part's, colour or not.
            pad = [0, 0, 0] if vertex_albedo is not None else []
            for k in range(1, len(pts) - 1):
                a, b, c = 0, k, k + 1
                all_px.append(np.array([[pxc[a], pxc[b], pxc[c]]]))
                all_py.append(np.array([[pyc[a], pyc[b], pyc[c]]]))
                all_iz.append(np.array([[izc[a], izc[b], izc[c]]]))
                # sentinel -2 in channel 0; u/z, v/z ride channels 1-2
                all_at.append(np.array([[
                    [-2.0, uvs[a][0] * izc[a], uvs[a][1] * izc[a], 0, 0, 0, *pad],
                    [-2.0, uvs[b][0] * izc[b], uvs[b][1] * izc[b], 0, 0, 0, *pad],
                    [-2.0, uvs[c][0] * izc[c], uvs[c][1] * izc[c], 0, 0, 0, *pad],
                ]]))

    if not all_px:
        return 0 if cost_only else Image.new("RGB", (width, height), _BG)

    px_all, py_all, iz_all = np.vstack(all_px), np.vstack(all_py), np.vstack(all_iz)
    if cost_only:
        return _pair_count(px_all, py_all, iz_all, width, height).total

    buf = _rasterize(
        px_all, py_all, iz_all,
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
    plate: bool = True,
    letterbox: bool = True,
    deadline: float | None = None,
    require_colors: bool = False,
) -> list[dict] | None:
    """Paint every requested view in the stage look, or ``None``.

    The contract is :func:`kiln.stage_still.try_render_stage_views`'s,
    verbatim: ``None`` — never a partial list, never an exception — means
    "run the next backend"; the caller's angle machinery rides through
    unchanged; a non-hex *color* declines rather than guessing.

    A part that carries its own colours (a painted or multicolour 3MF,
    whose payload has ``vertex_colors``) is painted in them, by the stage
    document's rule: the colours sit on a white base, untinted, and a
    *color* the caller asked for yields to them.  ``require_colors=True``
    declines when the payload carries none — a caller that knows the part
    is painted asks for that, because the part in one colour is the wrong
    picture, and a wrong picture in the stage look is worse than a right
    one without it.

    ``plate=False`` omits the print bed (grid, ember cross, stamp, and the
    contact shadow baked into its texture) so the part floats on the bare
    backdrop; ``letterbox=False`` omits the footer strip the live stage
    reserves for its own UI.  Both default True — the full stage look.
    They exist for surfaces that show the OBJECT rather than the print
    setup (library card thumbnails), where bed chrome at tile size reads
    as noise.  ``_paint_view`` already treats a ``None`` plate texture as
    "no plate", so the off switch is the absence of the texture, not a
    second code path.

    ``deadline`` is the caller's whole-call ceiling as a ``time.monotonic()``
    instant (:func:`kiln.model_visualizer.visualize_model` strikes one for
    every backend).  Under it the loop checks BETWEEN views whether another
    view the size of the last one still fits, and stops there: the views
    that did fit come back, and the caller reports the rest as skipped.
    That is the one case a partial list is the honest answer — measured
    2026-09-06, six 1600x1200 angles painted for ~50 s with no ceiling and
    pushed the tool call past the MCP host's window, so the host saw a
    timeout and the user saw nothing.  A view cannot be interrupted
    mid-raster, which is why the check is predictive.  Nothing painted
    before the deadline is ``None`` (next backend), same as any decline.
    Without a deadline the all-or-nothing contract holds unchanged.
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
        v, f, colours = loaded
        # The stage hands vertex colours to three.js as normalized bytes on
        # a white base (mesh_viewer.html, modelMaterial), and three reads a
        # colour attribute as linear, so the bytes are the linear albedo
        # here too -- no sRGB decode, or the painted part would come out
        # darker than the photograph of the same file.
        vertex_albedo = None
        if colours is not None:
            vertex_albedo = colours[:, :3].astype(_np.float64) / 255.0
        elif require_colors:
            logger.debug("stage paint: %s carries no colours to paint — declining", file_path)
            return None

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

        plate_tex_np = (
            _np.asarray(_plate_texture(footprint), dtype=_np.uint8)
            if plate
            else None
        )

        # Price every view before painting any.  The cap belongs to the
        # VIEW but the contract belongs to the SET, so one view over it
        # makes the whole set impossible — and discovering that on view
        # five costs the four already drawn (measured 2026-09-06: ~30 s
        # at 1600x1200, then all of it thrown away).  Projection is
        # arithmetic; only rasterizing is expensive.
        for label, _description in selected:
            rx, _ry, rz = rotations[label]
            az, el = _openscad_rotation_to_orbit(rx, rz)
            ss_probe = min(ss + 1, 4)
            probe_h = height * ss_probe
            strip_probe = round(_FOOTER_PX * ss_probe / ss) if letterbox else 0
            canvas_probe = probe_h - strip_probe
            if canvas_probe < 32:
                canvas_probe = probe_h
            cost = _paint_view(
                v, f, az, el,
                width=width * ss_probe, height=canvas_probe,
                albedo_lin=albedo_lin, floor_y=floor_y,
                footprint=footprint, fit_radius=radius, fit_size=fit_size,
                plate_tex_np=plate_tex_np, cost_only=True,
            )
            if cost > _PAIR_CAP:
                logger.debug(
                    "stage paint: %s would cost %d raster pairs, past the %d cap "
                    "— the whole set declines before painting anything",
                    label, cost, _PAIR_CAP,
                )
                return None

        views: list[dict] = []
        last_view_s = 0.0
        for label, description in selected:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or remaining < last_view_s:
                    logger.debug(
                        "stage paint: %.1fs left before the call deadline, last view "
                        "took %.1fs — stopping after %d/%d angle(s)",
                        remaining, last_view_s, len(views), len(selected),
                    )
                    break
            started = time.monotonic()
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
            strip = round(_FOOTER_PX * ss_int / ss) if letterbox else 0
            canvas_h = full_h - strip
            if canvas_h < 32:  # degenerate request: skip the letterbox
                canvas_h = full_h
            img = _paint_view(
                v, f, az, el,
                width=width * ss_int, height=canvas_h,
                albedo_lin=albedo_lin, floor_y=floor_y,
                footprint=footprint, fit_radius=radius, fit_size=fit_size,
                plate_tex_np=plate_tex_np, vertex_albedo=vertex_albedo,
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
            last_view_s = time.monotonic() - started
        if deadline is not None and not views:
            return None
        return views
    except Exception:  # noqa: BLE001 — a paint failure must never break a preview
        logger.debug("stage paint failed — falling through", exc_info=True)
        return None
