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
Nothing in the light rig is fitted.  The stage renders with three r160,
whose units ARE pixel units once you follow its pipeline to the end:
a light hands the shader ``color * intensity`` (no PI, the page leaves
``useLegacyLights`` off), the material is MeshPhysicalMaterial's own
lobe, and the frame is NOT tone-mapped.  With the bloom composer
present the page sets ``renderer.toneMapping = NoToneMapping``, and
three's OutputPass tone-maps only when the renderer does -- so the
screen gets the sRGB encoding of the linear sum, clipped at white.
Until 2026-09-23 this module ran ACES there and fitted its light
levels to make up the difference, which flattened every highlight
(measured on the sphere probe: the brightest tone band 45-88 levels
under the photograph; the transcription below is within 2.5 in every
band).  ``kiln/scripts/calibrate_stage_paint.py`` photographs the stage
and checks the transcription; re-run it whenever the stage document's
rig changes.

The rest of the frame is three's too: the environment through the
generator's own PMREM blur chain (:func:`_build_env_tables`), the
shader's specular anti-aliasing (:func:`_geometry_roughness`), and the
composer's UnrealBloomPass (:func:`_unreal_bloom`), which reproduces
three's pass to a byte on a frame three ran.

What remains approximate: PMREM runs on a lat-long grid rather than
cube faces, and the equirect's mip-mapped sampling on the way into the
cube is left out (it dims the zenith about 1% after the blur); the frame
is supersampled where the browser multisamples.  Together the painter
reads about 2% brighter than the stage on lit faces -- invisible in a
tone, but bloom has a hard threshold: a WHITE part whose lit faces sit
right at it glows several times as much as the photograph (the painted
jar's rim, 2026-09-23; the same frame 2% dimmer glows exactly as the
photograph does).  The environment gradient's SHAPE is
transcribed like everything else.  The BRDF itself is not approximated:
GGX, three's Schlick Fresnel, height-correlated Smith visibility.
Shading normals are the stage's own too: it creases every
payload that ships none (three's toCreasedNormals at 30 degrees), and so
does :func:`_creased_normals`, corner for corner.  Hidden
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

# Shading normals: `modelGeometry` gives every payload that ships no
# normals (every payload a stage door builds) `creasedNormals` -- three's
# `toCreasedNormals`, hand-ported between the document's
# `crease-normals:begin/end` markers -- and draws with `flatShading`
# off.  `PRINT_CREASE_DEG = 30`, the web viewer's crease; corners weld
# when `trunc(position * (1 + 1e-10) * 1e2)` agrees, a hundredth of a
# unit.  So curved walls shade round and hard rims stay hard.
_CREASE_DEG = 30.0
_CREASE_HASH = (1 + 1e-10) * 1e2

# Lights: `AmbientLight(0xffffff, 0.35)`, key `0xfff7ee @ 1.0` from
# (10, 20, 10), rim `0xd8e1ff @ 0.5` from (-15, 8, -10), graze
# `0xffffff @ 0.75` from (16, 6, 1.5), counter-graze `0xffffff @ 0.5`
# from (-16, 6, 1.5).  Positions are directions (normalized in-scene).
# Colours stay as the page writes them: three reads a hex colour as sRGB
# and decodes it to linear (:func:`_hex_linear`), which is what tints the
# rim as blue as it is.
_AMBIENT = (0xFFFFFF, 0.35)
_LIGHTS = (
    # (direction xyz, colour, intensity)
    ((10.0, 20.0, 10.0), 0xFFF7EE, 1.0),
    ((-15.0, 8.0, -10.0), 0xD8E1FF, 0.5),
    ((16.0, 6.0, 1.5), 0xFFFFFF, 0.75),
    ((-16.0, 6.0, 1.5), 0xFFFFFF, 0.5),
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
_ENV_CANVAS_W = 256  # buildEnvMap: `cv.width = 256`

# PMREM: `new THREE.PMREMGenerator(r).fromEquirectangular(equirect)`, the
# generator of three r160 as vendored.  It sizes its cube at a quarter of
# the texture's width (`_setSize(texture.image.width / 4)`); the rest is
# its own: LOD_MIN 4, EXTRA_LOD_SIGMA, MAX_SAMPLES 20, and the ten pole
# axes it cycles its blurs through.
_PMREM_LOD_MIN = 4
_PMREM_EXTRA_SIGMA = (0.125, 0.215, 0.35, 0.446, 0.526, 0.582)
_PMREM_MAX_SAMPLES = 20
_PHI = (1.0 + math.sqrt(5.0)) / 2.0
_PMREM_AXES = (
    (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0), (1.0, 1.0, -1.0), (-1.0, 1.0, -1.0),
    (0.0, _PHI, 1.0 / _PHI), (0.0, _PHI, -1.0 / _PHI),
    (1.0 / _PHI, 0.0, _PHI), (-1.0 / _PHI, 0.0, _PHI),
    (_PHI, 1.0 / _PHI, 0.0), (-_PHI, 1.0 / _PHI, 0.0),
)
# textureCubeUV's roughness -> mip knots (`cubeUV_r0/m0` .. `cubeUV_r6/m6`).
_CUBEUV_KNOTS = ((1.0, -2.0), (0.8, -1.0), (0.4, 2.0), (0.305, 3.0), (0.21, 4.0))
#: The lat-long grid the blur chain runs on.  128 x 64 lands within
#: 0.002 (linear) of a 512 x 256 run at a sixteenth of the cost -- under a
#: quarter of a tone level -- so finer buys nothing a PNG can show.
_PMREM_GRID = (128, 64)

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

_METALNESS = 0.05  # MeshPhysicalMaterial metalness, transcribed

#: `envMapIntensity`: MeshPhysicalMaterial's default, which the page
#: never sets.
_ENV_INTENSITY = 1.0

# Bloom: `composer.addPass(new THREE.UnrealBloomPass(new THREE.Vector2(w,
# h), BLOOM_STRENGTH, BLOOM_RADIUS, BLOOM_THRESHOLD))` with
# `BLOOM_STRENGTH = 0.45, BLOOM_RADIUS = 0.85, BLOOM_THRESHOLD = 0.92`
# (a still never runs the reveal, so its pulse never adds to strength).
# The rest is the pass's own, from the vendored UnrealBloomPass.js: the
# high pass's `smoothWidth` 0.01, five mips with `kernelSizeArray = [3,
# 5, 7, 9, 11]`, and `bloomFactors = [1.0, 0.8, 0.6, 0.4, 0.2]`.
_BLOOM_STRENGTH = 0.45
_BLOOM_RADIUS = 0.85
_BLOOM_THRESHOLD = 0.92
_BLOOM_SMOOTH_WIDTH = 0.01
_BLOOM_KERNELS = (3, 5, 7, 9, 11)
_BLOOM_FACTORS = (1.0, 0.8, 0.6, 0.4, 0.2)
_LUMA = (0.299, 0.587, 0.114)  # LuminosityHighPassShader's weights


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:  # noqa: F821
    return _np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:  # noqa: F821
    c = _np.clip(c, 0.0, 1.0)
    return _np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _hex_linear(value: int):
    """A hex colour as three holds it: sRGB bytes, decoded to linear."""
    rgb = _np.array([(value >> 16) & 255, (value >> 8) & 255, value & 255], dtype=_np.float64)
    return _srgb_to_linear(rgb / 255.0)


#: ``(diffuse, levels)`` lookup tables over the direction's y, built once.
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


def _pmrem_plan():
    """``(lod_max, sizes, sigmas)``: the generator's ``_createPlanes``."""
    lod_max = int(math.floor(math.log2(_ENV_CANVAS_W / 4)))
    sizes, sigmas = [], []
    lod = lod_max
    for i in range(lod_max - _PMREM_LOD_MIN + 1 + len(_PMREM_EXTRA_SIGMA)):
        size = 2 ** lod
        sigma = 1.0 / size
        if i > lod_max - _PMREM_LOD_MIN:
            sigma = _PMREM_EXTRA_SIGMA[i - lod_max + _PMREM_LOD_MIN - 1]
        elif i == 0:
            sigma = 0.0
        sizes.append(size)
        sigmas.append(sigma)
        if lod > _PMREM_LOD_MIN:
            lod -= 1
    return lod_max, sizes, sigmas


def _roughness_to_mip(roughness, lod_max: int):
    """``clamp(roughnessToMip(roughness), cubeUV_m0, CUBEUV_MAX_MIP)``, elementwise."""
    np = _np
    r = np.asarray(roughness, dtype=np.float64)
    mip = -2.0 * np.log2(1.16 * np.maximum(r, 1e-6))
    # The shader's if-chain, applied last branch first so earlier ones win.
    knots = list(zip(_CUBEUV_KNOTS, _CUBEUV_KNOTS[1:], strict=False))
    for (r_a, m_a), (r_b, m_b) in reversed(knots):
        mip = np.where(r >= r_b, (r_a - r) * (m_b - m_a) / (r_a - r_b) + m_a, mip)
    return np.clip(mip, _CUBEUV_KNOTS[0][1], float(lod_max))


def _env_level_lookup(levels, y, mip, lod_max: int):
    """``textureCubeUV`` from the level tables: two levels, mixed by the mip's fraction."""
    np = _np
    mip_int = np.floor(mip)
    frac = (mip - mip_int)[:, None]
    coarse = (lod_max - mip_int).astype(np.int64)  # mip m is level lod_max - m
    fine = np.maximum(coarse - 1, 0)
    idx = np.clip((y + 1.0) * 0.5 * (_ENV_TABLE_N - 1), 0, _ENV_TABLE_N - 1.001)
    lo = idx.astype(np.int64)
    t = (idx - lo)[:, None]

    def at(level):
        return levels[level, lo] * (1.0 - t) + levels[level, lo + 1] * t

    return at(coarse) * (1.0 - frac) + at(fine) * frac


def _latlong_sample(field, dirs):
    """Bilinear lookup of a lat-long *field* ``(rows, cols, 3)`` at unit *dirs*."""
    np = _np
    rows, cols = field.shape[:2]
    lat = np.arcsin(np.clip(dirs[..., 1], -1.0, 1.0))
    lon = np.arctan2(dirs[..., 0], dirs[..., 2]) % (2.0 * math.pi)
    y = (lat / math.pi + 0.5) * rows - 0.5
    x = lon / (2.0 * math.pi) * cols - 0.5
    y0, x0 = np.floor(y), np.floor(x)
    ty, tx = (y - y0)[..., None], (x - x0)[..., None]
    ya = np.clip(y0.astype(np.int64), 0, rows - 1)
    yb = np.clip(y0.astype(np.int64) + 1, 0, rows - 1)
    xa = x0.astype(np.int64) % cols
    xb = (xa + 1) % cols
    top = field[ya, xa] * (1.0 - tx) + field[ya, xb] * tx
    bottom = field[yb, xa] * (1.0 - tx) + field[yb, xb] * tx
    return top * (1.0 - ty) + bottom * ty


def _pmrem_half_blur(field, dirs, sigma, size_in, pole, latitudinal):
    """One ``_halfBlur`` pass: a 1-D Gaussian of rotations about an axis.

    Latitudinal rotates each direction about the pole axis itself,
    longitudinal about ``cross(pole, direction)``; the step is a texel of
    the level read (``PI / (2 * (size - 1))``), and the taps and weights
    are the generator's -- ``1 + floor(3 * sigmaPixels)`` of them, capped
    at ``MAX_SAMPLES``, normalized over the ones the shader reads.
    """
    np = _np
    d_theta = math.pi / (2 * (size_in - 1))
    sigma_px = sigma / d_theta
    samples = min(1 + int(math.floor(3.0 * sigma_px)), _PMREM_MAX_SAMPLES)
    weights = [math.exp(-0.5 * (i / sigma_px) ** 2) for i in range(samples)]
    total = weights[0] + 2.0 * sum(weights[1:])
    pole = np.asarray(pole, dtype=np.float64)
    if latitudinal:
        axis = np.broadcast_to(pole, dirs.shape).copy()
    else:
        axis = np.cross(pole, dirs)
        flat = ~axis.any(axis=-1)
        axis[flat] = np.stack(
            [dirs[flat][:, 2], np.zeros(int(flat.sum())), -dirs[flat][:, 0]], axis=-1
        )
    axis /= np.linalg.norm(axis, axis=-1, keepdims=True)
    along = (axis * dirs).sum(axis=-1, keepdims=True)
    across = np.cross(axis, dirs)
    out = weights[0] * _latlong_sample(field, dirs)
    for i in range(1, samples):
        for theta in (-d_theta * i, d_theta * i):
            turned = (dirs * math.cos(theta) + across * math.sin(theta)
                      + axis * along * (1.0 - math.cos(theta)))
            out += weights[i] * _latlong_sample(field, turned)
    return out / total


def _build_env_tables():
    """The gradient through three's PMREM chain, read at two roughnesses.

    ``getIBLIrradiance`` reads the chain at roughness 1 and returns
    ``PI * envColor``, which the diffuse BRDF's 1/PI cancels, so the
    indirect diffuse is ``envColor * diffuseColor``; ``getIBLRadiance``
    reads it at the material's roughness along the bent reflection.  Both
    are whatever PMREM's blurs made of the gradient -- NOT the cosine and
    GGX integrals they stand in for.  The roughest level is a Gaussian
    about 33 degrees wide, far narrower than a cosine lobe, so it sees
    more of the white zenith from an up-facing normal: exact integrals
    there (as this used until 2026-09-23) put every top face several tone
    levels under the photograph.

    The chain runs as the generator runs it -- each level blurred from the
    last by ``sqrt(sigma_i^2 - sigma_(i-1)^2)``, latitudinal then
    longitudinal, about the next of its ten pole axes -- on a lat-long
    grid rather than cube faces.  The gradient varies with elevation
    alone and the result stays within 0.02 of that symmetry, so each level
    is averaged over azimuth into a table over the direction's y.
    """
    np = _np
    lod_max, sizes, sigmas = _pmrem_plan()
    cols, rows = _PMREM_GRID
    lat = ((np.arange(rows) + 0.5) / rows - 0.5) * math.pi
    lon = (np.arange(cols) + 0.5) / cols * 2.0 * math.pi
    lat_g, lon_g = np.meshgrid(lat, lon, indexing="ij")
    dirs = np.stack([np.cos(lat_g) * np.sin(lon_g), np.sin(lat_g),
                     np.cos(lat_g) * np.cos(lon_g)], axis=-1)

    field = _env_radiance_at(dirs[..., 1].reshape(-1)).reshape(rows, cols, 3)
    levels = [field.mean(axis=1)]
    for i in range(1, len(sizes)):
        sigma = math.sqrt(sigmas[i] ** 2 - sigmas[i - 1] ** 2)
        pole = _PMREM_AXES[(i - 1) % len(_PMREM_AXES)]
        field = _pmrem_half_blur(field, dirs, sigma, sizes[i - 1], pole, True)
        field = _pmrem_half_blur(field, dirs, sigma, sizes[i], pole, False)
        levels.append(field.mean(axis=1))

    knots = np.linspace(-1.0, 1.0, _ENV_TABLE_N)
    lat_y = np.sin(lat)
    tables = np.stack([
        np.stack([np.interp(knots, lat_y, level[:, ch]) for ch in range(3)], axis=-1)
        for level in levels
    ])
    diffuse = _env_level_lookup(
        tables, knots, np.full(len(knots), _roughness_to_mip(1.0, lod_max)), lod_max
    )
    return diffuse, tables


def _env_tables():
    """``(diffuse, levels)``: the roughness-1 table and every PMREM level, built once."""
    global _ENV_TABLES
    if _ENV_TABLES is None:
        _ENV_TABLES = _build_env_tables()
    return _ENV_TABLES


def _env_lookup(table, y):
    """Linear RGB from a direction-y table, linearly interpolated."""
    idx = _np.clip((y + 1.0) * 0.5 * (_ENV_TABLE_N - 1), 0, _ENV_TABLE_N - 1.001)
    lo = idx.astype(_np.int64)
    t = (idx - lo)[:, None]
    return table[lo] * (1.0 - t) + table[lo + 1] * t


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
    """``(vertices, faces, colours, normals)`` in the stage's y-up frame, or ``None``.

    Read from the stage's own payload door (:func:`kiln.local_stage.
    _payload_for_mesh`), the one the live panel and the photograph draw
    from, so the painter paints what the stage shows.  It used to call
    ``trimesh.load`` itself, which cost it two things: a painted part's
    colours, which trimesh never reads from a 3MF, so every painted part
    skipped this backend for the grey renderer; and every 3MF on a plain
    install, where trimesh's 3MF loader lacks the libraries it needs and
    the payload's own reader does not.

    ``colours`` is the payload's RGBA per vertex, or ``None`` for a part
    that carries none; ``normals`` likewise, and ``None`` is the default --
    no stage door asks the encoder for them, so the stage creases its own
    (:func:`_creased_normals`).  Positions arrive already rotated into the viewer
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
    normals = None
    if payload.get("normals"):
        nrm = _np.frombuffer(base64.b64decode(payload["normals"]), dtype="<f4")
        if len(nrm) == len(v) * 3:
            normals = nrm.reshape(-1, 3)
    return v, f, colours, normals


def _bounding_sphere(v):
    """three.js ``computeBoundingSphere``: bbox centre, max vertex distance."""
    lo, hi = v.min(axis=0), v.max(axis=0)
    c = (lo + hi) / 2.0
    r = float(_np.sqrt(((v - c) ** 2).sum(axis=1).max()))
    return c, max(r, 1e-6), lo, hi


#: Ceiling on the crease pass's pairwise work: the sum, over welded
#: vertices, of the corners there squared.  An ordinary mesh spends a few
#: tens per face (a 327k-face sphere creases in 0.2 s); only a fan of
#: tens of thousands of faces on one point gets near it -- 2e8 pairs
#: measured 0.18 s, so this sits near two seconds -- and past it the
#: part falls through rather than stall a preview.  The stage's own port
#: pays the same quadratic, in JavaScript.
_CREASE_MAX_PAIRS = 2_000_000_000
_CREASE_SLAB = 4_000_000  # pair-matrix entries held at once


def _creased_normals(v, f):
    """Per-corner shading normals ``(F, 3, 3)``, the stage's rule, or ``None``.

    ``creasedNormals`` verbatim: each face's normal is ``(c - b) x (a - b)``
    normalized; corners whose ``trunc(position * _CREASE_HASH)`` agree
    are one vertex; a corner's normal is the UNWEIGHTED mean of the
    normals of the faces around its vertex -- a face counted once per
    corner it has there -- that meet its own face within
    ``_CREASE_DEG``, normalized.  *v* must be the payload's positions as
    sent: the stage welds before it centres, so a centred mesh would
    bin its corners differently.

    Grouping is a value sort, not a byte-wise ``unique``: ``-0.0`` and
    ``0.0`` are one bin to the stage's ``===`` and must be here.
    ``None`` when the pairwise work is past ``_CREASE_MAX_PAIRS``.
    """
    np = _np
    tri = v[f]
    fn = np.cross(tri[:, 2] - tri[:, 1], tri[:, 0] - tri[:, 1])
    ln = np.linalg.norm(fn, axis=1)
    fn = fn / np.where(ln > 0.0, ln, 1.0)[:, None]  # the port's `|| 1`
    crease = math.cos(_CREASE_DEG * math.pi / 180.0)

    q = np.trunc(tri.reshape(-1, 3) * _CREASE_HASH)
    order = np.lexsort((q[:, 2], q[:, 1], q[:, 0]))
    qs = q[order]
    fresh = np.ones(len(qs), dtype=bool)
    fresh[1:] = (qs[1:] != qs[:-1]).any(axis=1)
    starts = np.flatnonzero(fresh)
    sizes = np.diff(np.append(starts, len(qs)))
    if int((sizes.astype(np.int64) ** 2).sum()) > _CREASE_MAX_PAIRS:
        return None

    face_of = order // 3  # the face each sorted corner belongs to
    out = np.empty((len(q), 3), dtype=np.float32)  # the port's Float32Array
    for d in np.unique(sizes):
        d = int(d)
        group_starts = starts[sizes == d]
        if d * d <= _CREASE_SLAB:
            # Every vertex with d corners at once: (g, d, 3) normals, a
            # (g, d, d) crease mask, one batched product for the sums.
            step = max(1, _CREASE_SLAB // (d * d))
            for s in range(0, len(group_starts), step):
                idx = group_starts[s:s + step, None] + np.arange(d)[None, :]
                nrm = fn[face_of[idx]]
                within = nrm @ nrm.transpose(0, 2, 1) > crease
                sums = within.astype(np.float64) @ nrm
                norm = np.linalg.norm(sums, axis=2)
                out[order[idx]] = sums / np.where(norm > 0.0, norm, 1.0)[..., None]
        else:
            # One crowded vertex at a time, in row slabs.
            rows = max(1, _CREASE_SLAB // d)
            for g0 in group_starts:
                members = order[g0:g0 + d]
                nrm = fn[face_of[g0:g0 + d]]
                for r0 in range(0, d, rows):
                    part = nrm[r0:r0 + rows]
                    sums = (part @ nrm.T > crease).astype(np.float64) @ nrm
                    norm = np.linalg.norm(sums, axis=1)
                    out[members[r0:r0 + rows]] = (
                        sums / np.where(norm > 0.0, norm, 1.0)[:, None]
                    )
    return out.reshape(-1, 3, 3)


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


def _camera_basis(eye):
    """``(right, up, forward)`` of a camera at *eye* looking at the origin."""
    fwd = -eye / _np.linalg.norm(eye)
    up = _np.array([0.0, 1.0, 0.0])
    right = _np.cross(fwd, up)
    nr = _np.linalg.norm(right)
    if nr < 1e-9:  # straight up/down: pick a stable right-hand basis
        right = _np.array([1.0, 0.0, 0.0])
        nr = 1.0
    right = right / nr
    return right, _np.cross(right, fwd), fwd


def _view_projection(eye, w: int, h: int):
    """World → pixel mapping for a camera at *eye* looking at the origin."""
    right, cam_up, fwd = _camera_basis(eye)
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


def _shade(albedo_lin, normals, view, geometry_roughness=None):
    """Per-pixel LINEAR radiance: three r160's MeshPhysicalMaterial, verbatim.

    ``RE_Direct_Physical`` per directional light, ``RE_IndirectDiffuse``
    for the ambient, ``RE_IndirectSpecular_Physical`` for the environment
    -- every term at the level three computes it, nothing fitted.  The
    result is the linear frame three renders into the composer's target;
    :func:`_develop` finishes it the way the composer does.

    The two lessons this carries.  The direct SPECULAR is added after the
    diffuse, never multiplied by the part's colour: ``directSpecular +=
    irradiance * BRDF_GGX`` beside ``directDiffuse += irradiance *
    diffuseColor / PI``.  Tinting it (as this did until 2026-09-23) turned
    a highlight on a painted part the part's colour and cost it the PI it
    has over the diffuse; on a 60-gon's wall the highlight stood 15.1 tone
    levels proud of the wall where the photograph's stands 25.4.  And the environment
    is the only light a DOWNWARD face gets -- the four lights all arrive
    from above -- so its two convolutions (:func:`_env_tables`) vary, the
    diffuse half with the normal and the specular half with the
    REFLECTION; a flat ambient in their place put a bottom view 40 tone
    levels under the photograph.

    *albedo_lin* is one linear RGB for the whole part, or one per pixel
    ``(N, 3)`` for a part carrying its own colours; the Fresnel base
    follows it per channel, as ``material.specularColor`` does.
    *geometry_roughness* is three's per-pixel ``geometryRoughness``
    (:func:`_geometry_roughness`), added to the material's roughness as
    the shader adds it; ``None`` is a surface that does not curve.
    """
    np = _np
    roughness = np.full(len(normals), max(_ROUGHNESS, 0.0525))
    if geometry_roughness is not None:
        roughness = np.minimum(roughness + geometry_roughness, 1.0)
    alpha = roughness * roughness
    a2 = alpha * alpha
    albedo = albedo_lin if albedo_lin.ndim == 2 else albedo_lin[None, :]
    diffuse = albedo * (1.0 - _METALNESS)
    # specularColor = mix(0.04, diffuseColor, metalness): IOR 1.5 and a
    # white specularColor put the dielectric base at ((1.5-1)/(1.5+1))^2.
    f0 = 0.04 + (albedo - 0.04) * _METALNESS

    ndv = (normals * view).sum(axis=1)
    nv = np.clip(ndv, 0.0, 1.0)

    irradiance = np.zeros((len(normals), 3))
    specular = np.zeros((len(normals), 3))
    for direction, colour, intensity in _LIGHTS:
        ldir = np.asarray(direction, dtype=np.float64)
        ldir = ldir / np.linalg.norm(ldir)
        ndl = np.clip(normals @ ldir, 0.0, 1.0)
        half = ldir[None, :] + view
        half = half / np.maximum(np.linalg.norm(half, axis=1), 1e-12)[:, None]
        ndh = np.clip((normals * half).sum(axis=1), 0.0, 1.0)
        vdh = np.clip((view * half).sum(axis=1), 0.0, 1.0)
        fresnel = np.exp2((-5.55473 * vdh - 6.98316) * vdh)[:, None]
        f = f0 * (1.0 - fresnel) + fresnel  # specularF90 is 1
        gv = ndl * np.sqrt(a2 + (1.0 - a2) * nv * nv)
        gl = nv * np.sqrt(a2 + (1.0 - a2) * ndl * ndl)
        vis = 0.5 / np.maximum(gv + gl, 1e-6)
        dist = a2 / (math.pi * (ndh * ndh * (a2 - 1.0) + 1.0) ** 2)
        light = ndl[:, None] * (_hex_linear(colour) * intensity)[None, :]
        irradiance += light
        specular += light * f * (vis * dist)[:, None]
    ambient_colour, ambient_intensity = _AMBIENT
    irradiance += _hex_linear(ambient_colour) * ambient_intensity
    color = irradiance * diffuse / math.pi + specular

    # The environment.  DFGApprox is the split-sum term, the multi-scatter
    # compensation follows, and the diffuse half is attenuated by what the
    # specular half took.
    diff_tbl, levels = _env_tables()
    env_d = _ENV_INTENSITY * _env_lookup(diff_tbl, normals[:, 1])
    # reflect(-view, normal) on the RAW dot: a smooth normal near the
    # silhouette can face away from the eye, and three does not clamp here.
    refl = 2.0 * ndv[:, None] * normals - view
    refl = refl + (normals - refl) * alpha[:, None]  # mix(reflectVec, normal, roughness^2)
    refl = refl / np.maximum(np.linalg.norm(refl, axis=1), 1e-12)[:, None]
    lod_max = _pmrem_plan()[0]
    env_s = _ENV_INTENSITY * _env_level_lookup(
        levels, refl[:, 1], _roughness_to_mip(roughness, lod_max), lod_max
    )
    r_x = 1.0 - roughness
    a004 = np.minimum(r_x * r_x, np.exp2(-9.28 * nv)) * r_x + (-0.0275 * roughness + 0.0425)
    fab_x = (-1.04 * a004 + (-0.572 * roughness + 1.04))[:, None]
    fab_y = (1.04 * a004 + (0.022 * roughness - 0.04))[:, None]
    fss_ess = f0 * fab_x + fab_y
    ems = 1.0 - (fab_x + fab_y)
    favg = f0 + (1.0 - f0) * 0.047619
    multi = fss_ess * favg / (1.0 - ems * favg) * ems
    scatter = (fss_ess + multi).max(axis=1)
    color += diffuse * (1.0 - scatter)[:, None] * env_d
    color += env_s * fss_ess + multi * env_d
    return color


def _geometry_roughness(tris_px, tris_py, tris_invz, attrs, tri, raw, length,
                        normal, inv_z, basis, px_per_device):
    """three's ``geometryRoughness``, per pixel: how fast the normal turns.

    ``lights_physical_fragment`` does ``dxy = max(abs(dFdx(normal)),
    abs(dFdy(normal)))`` on the view-space normal, takes its largest
    component and ADDS it to the material's roughness -- specular
    anti-aliasing.  Where a surface curves tightly across a pixel (a rim,
    a fillet, the edge of a carved letter) the highlight is spread out
    and damped; left out, those highlights stay mirror-sharp, blow past
    white and feed the bloom a glow the stage does not have.

    The GPU's derivative is the pixel quad's difference on the pixel's own
    triangle (helper pixels extend it past the edge), which is this: the
    interpolated normal's exact screen gradient on that triangle,
    perspective-correct, normalized as the shader normalizes, scaled from
    this raster's pixels to the browser's (*px_per_device*).
    """
    np = _np
    ax, ay = tris_px[tri, 0], tris_py[tri, 0]
    bx, by = tris_px[tri, 1], tris_py[tri, 1]
    qx, qy = tris_px[tri, 2], tris_py[tri, 2]
    area = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax)
    area = np.where(np.abs(area) > 1e-12, area, 1e-12)
    # Screen gradients of the rasterizer's own barycentrics (w_i / area).
    gx0, gy0 = (by - qy) / area, (qx - bx) / area
    gx1, gy1 = (qy - ay) / area, (ax - qx) / area
    gradients = ((gx0, gx1, -(gx0 + gx1)), (gy0, gy1, -(gy0 + gy1)))
    corners = attrs[tri, :, 0:3]  # normal / z at each corner
    iz = tris_invz[tri]
    worst = np.zeros(len(tri))
    for g0, g1, g2 in gradients:
        d_num = (g0[:, None] * corners[:, 0] + g1[:, None] * corners[:, 1]
                 + g2[:, None] * corners[:, 2])
        d_den = g0 * iz[:, 0] + g1 * iz[:, 1] + g2 * iz[:, 2]
        d_raw = (d_num - raw * d_den[:, None]) / inv_z[:, None]
        d_n = d_raw - normal * (normal * d_raw).sum(axis=1)[:, None]
        d_n *= (px_per_device / length)[:, None]
        for axis in basis:
            worst = np.maximum(worst, np.abs(d_n @ axis))
    return worst


def _js_round(x: float) -> int:
    """``Math.round``: halves go UP, where Python's ``round`` goes to even."""
    return int(math.floor(x + 0.5))


def _sample_axis(img, axis: int, n_out: int, offset: float = 0.0):
    """Sample *img* along one axis the way a GPU texture fetch does.

    ``LinearFilter`` with ``ClampToEdgeWrapping`` (three's render-target
    default), at the centres of an *n_out*-texel target shifted by
    *offset* of ITS texels -- which is how the blur passes step, in
    units of the target they draw into, whatever the size they read.
    Bilinear sampling is separable, so two calls make one 2-D fetch.
    """
    np = _np
    n_src = img.shape[axis]
    x = (np.arange(n_out) + 0.5 + offset) * (n_src / n_out) - 0.5
    x0 = np.floor(x)
    frac = (x - x0).astype(img.dtype)
    i0 = np.clip(x0.astype(np.int64), 0, n_src - 1)
    i1 = np.clip(x0.astype(np.int64) + 1, 0, n_src - 1)
    shape = [1] * img.ndim
    shape[axis] = n_out
    frac = frac.reshape(shape)
    return np.take(img, i0, axis=axis) * (1 - frac) + np.take(img, i1, axis=axis) * frac


def _sample(img, rows: int, cols: int):
    """A bilinear fetch of *img* at every centre of a rows x cols target."""
    return _sample_axis(_sample_axis(img, 0, rows), 1, cols)


def _resize_box(img, cols: int, rows: int):
    """Area-average *img* to cols x rows, channel by channel."""
    from PIL import Image

    np = _np
    out = np.empty((rows, cols, img.shape[2]), dtype=np.float32)
    for ch in range(img.shape[2]):
        band = Image.fromarray(np.ascontiguousarray(img[..., ch], dtype=np.float32), "F")
        out[..., ch] = np.asarray(band.resize((cols, rows), Image.BOX))
    return out


def _unreal_bloom(hdr, device_size):
    """three r160's UnrealBloomPass over a linear frame: the light it adds.

    The composer runs the pass on the BROWSER's canvas, *device_size*
    pixels, so every radius below is in those pixels; the painter's own
    supersampled frame is area-averaged down to it first, as the
    browser's multisample resolve does, in linear light.  Then, step for
    step:

    1. the luminosity high pass, into a half-size target: a bilinear
       fetch of the frame, kept by ``smoothstep(threshold, threshold +
       smoothWidth, luma)``;
    2. five mips, each a separable Gaussian (``KERNEL_RADIUS`` taps of
       ``0.39894 * exp(-0.5 i^2 / r^2) / r``, normalized) drawn into a
       target half the size of the last -- the horizontal pass fetches
       the previous mip bilinearly, which is where the downsampling
       happens;
    3. the composite, at the first mip's size: each mip weighted by
       ``mix(factor, 1.2 - factor, radius)``, times strength;
    4. the blend, ``AdditiveBlending`` from a ShaderMaterial, so
       ``blendFunc(SRC_ALPHA, ONE)``: the composite is added times its
       own alpha, and every blur target stores alpha 1, so that alpha is
       strength times the summed weights (1.35 here), not 1.

    Returned at *hdr*'s resolution, sampled bilinearly from the composite
    as the blend's full-screen quad samples it; ``None`` when nothing in
    the frame reaches the threshold.
    """
    np = _np
    dev_w, dev_h = device_size
    rows, cols = hdr.shape[:2]
    frame = hdr if (cols, rows) == (dev_w, dev_h) else _resize_box(hdr, dev_w, dev_h)
    w, h = max(1, _js_round(dev_w / 2)), max(1, _js_round(dev_h / 2))

    bright = _sample(frame, h, w)
    luma = bright @ np.asarray(_LUMA, dtype=bright.dtype)
    t = np.clip((luma - _BLOOM_THRESHOLD) / _BLOOM_SMOOTH_WIDTH, 0.0, 1.0)
    keep = t * t * (3.0 - 2.0 * t)
    if not keep.any():
        return None
    source = bright * keep[..., None]

    mips = []
    size = (w, h)
    for radius in _BLOOM_KERNELS:
        mw, mh = size
        coeff = [0.39894 * math.exp(-0.5 * i * i / (radius * radius)) / radius
                 for i in range(radius)]
        total = coeff[0] + 2.0 * sum(coeff[1:])
        rows_in = _sample_axis(source, 0, mh)
        across = coeff[0] * _sample_axis(rows_in, 1, mw)
        for i in range(1, radius):
            across += coeff[i] * (_sample_axis(rows_in, 1, mw, i)
                                  + _sample_axis(rows_in, 1, mw, -i))
        across /= total
        down = coeff[0] * across
        for i in range(1, radius):
            down += coeff[i] * (_sample_axis(across, 0, mh, i)
                                + _sample_axis(across, 0, mh, -i))
        down /= total
        mips.append(down)
        source = down
        size = (max(1, _js_round(mw / 2)), max(1, _js_round(mh / 2)))

    weights = [f + (1.2 - 2.0 * f) * _BLOOM_RADIUS for f in _BLOOM_FACTORS]
    composite = np.zeros((h, w, hdr.shape[2]), dtype=np.float32)
    for mip, weight in zip(mips, weights, strict=True):
        composite += weight * _sample(mip, h, w)
    composite *= _BLOOM_STRENGTH
    blend_alpha = _BLOOM_STRENGTH * sum(weights)
    return _sample(composite * blend_alpha, rows, cols)


#: Rows developed at once, so the tone step's temporaries track a slab of
#: the frame rather than all of it.
_DEVELOP_ROWS = 256


def _develop(hdr, device_size):
    """The composer's tail on the linear frame: bloom, then OutputPass.

    OutputPass does ``sRGBTransferOETF`` and nothing else here -- the
    renderer's ``toneMapping`` is ``NoToneMapping`` whenever the composer
    exists -- and the 8-bit canvas clips at white.  Returns sRGB bytes.
    """
    np = _np
    bloom = _unreal_bloom(hdr, device_size)
    out = np.empty(hdr.shape, dtype=np.uint8)
    for r0 in range(0, hdr.shape[0], _DEVELOP_ROWS):
        slab = hdr[r0:r0 + _DEVELOP_ROWS]
        if bloom is not None:
            slab = slab + bloom[r0:r0 + _DEVELOP_ROWS]
        out[r0:r0 + _DEVELOP_ROWS] = (_linear_to_srgb(slab) * 255.0 + 0.5).astype(np.uint8)
    return out


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
               w, h, pair_cap=None, device_scale=1.0):
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
    STRICTLY nearer depth — so the output is bit-identical.)  Returns the
    (h, w, 3) float32 LINEAR frame -- the composer's render target, which
    :func:`_develop` finishes -- or ``None`` when the pair budget says this
    frame is too heavy to paint honestly (with slicing that is a TIME
    bound; memory no longer scales with the total).
    """
    np = _np
    if pair_cap is None:
        pair_cap = _PAIR_CAP
    x0, y0, bw, counts, total = _pair_count(tris_px, tris_py, tris_invz, w, h)
    # The plate and the backdrop are sRGB bytes, decoded into the linear
    # frame by table: every byte comes back out of _develop as itself.
    byte_linear = _srgb_to_linear(np.arange(256) / 255.0).astype(np.float32)
    empty = np.empty((h, w, 3), dtype=np.float32)
    empty[:] = byte_linear[np.asarray(_BG)]
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
    basis = _camera_basis(eye)
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

        rgb = np.empty((len(hit), 3), dtype=np.float32)
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
            rgb[textured] = byte_linear[np.clip(top * (1 - ty) + bot * ty + 0.5,
                                                0, 255).astype(np.uint8)]
        smooth = ~textured
        if smooth.any():
            raw = a_interp[smooth, 0:3]
            ln = np.maximum(np.linalg.norm(raw, axis=1), 1e-12)
            n = raw / ln[:, None]
            pos = a_interp[smooth, 3:6]
            view = eye[None, :] - pos
            view = view / np.maximum(
                np.linalg.norm(view, axis=1), 1e-12)[:, None]
            albedo = a_interp[smooth, 6:9] if attrs.shape[2] > 6 else albedo_lin
            rough = _geometry_roughness(
                tris_px, tris_py, tris_invz, attrs, t_sel[smooth], raw, ln, n,
                izs[smooth], basis, 1.0 / device_scale,
            )
            rgb[smooth] = _shade(albedo, n, view, rough)

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
                corner_normals, cost_only=False, vertex_albedo=None,
                device_scale=1.0):
    """One still at full working resolution.  PIL image, or ``None``.

    ``cost_only`` stops after the geometry — every projection and clip
    the real pass makes, none of the rasterizing — and returns the view's
    pair count as an int.  That is what the set's pre-pass asks with, so
    the price it is quoted is the price the rasterizer will charge.

    ``vertex_albedo`` is the part's own linear RGB per vertex, or ``None``
    to paint the whole part in ``albedo_lin``.  ``corner_normals`` is the
    shading normal at each face's corners, ``(F, 3, 3)`` aligned with *f*
    (:func:`_creased_normals`).  ``device_scale`` is the browser canvas's
    size over this frame's: the bloom's radii are in its pixels.
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
    cn = corner_normals[keep]
    fn = fn[keep] / ln[keep][:, None]
    centroids = tri[keep].mean(axis=1)
    facing = ((eye[None, :] - centroids) * fn).sum(axis=1) > 0
    f2 = f2[facing]

    all_px, all_py, all_iz, all_at = [], [], [], []

    if len(f2):
        tri = v[f2]
        # Each corner carries the stage's creased normal and the
        # interpolator blends them across the face, as three's vertex
        # normals are: round walls, hard rims.  (Until 2026-09-22 the
        # stage flat-shaded and so did this, the face normal on all three.)
        tn = cn[facing].astype(_np.float64)
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

    hdr = _rasterize(
        px_all, py_all, iz_all,
        np.vstack(all_at), plate_tex_np, albedo_lin, eye, width, height,
        device_scale=device_scale,
    )
    if hdr is None:
        return None
    device = (_js_round(width * device_scale), _js_round(height * device_scale))
    return Image.fromarray(_develop(hdr, device), "RGB")


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
        v, f, colours, normals = loaded
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

        # Shading normals before centring: the stage welds corners on the
        # payload's positions as sent, and a shifted mesh bins differently.
        corner_normals = normals[f] if normals is not None else _creased_normals(v, f)
        if corner_normals is None:
            logger.debug("stage paint: %s is past the crease budget", file_path)
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
                plate_tex_np=plate_tex_np, corner_normals=corner_normals,
                cost_only=True,
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
            # The browser draws this canvas at the user's supersample
            # (width * ss across), so that is the frame its bloom sees.
            img = _paint_view(
                v, f, az, el,
                width=width * ss_int, height=canvas_h,
                albedo_lin=albedo_lin, floor_y=floor_y,
                footprint=footprint, fit_radius=radius, fit_size=fit_size,
                plate_tex_np=plate_tex_np, corner_normals=corner_normals,
                vertex_albedo=vertex_albedo, device_scale=ss / ss_int,
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
