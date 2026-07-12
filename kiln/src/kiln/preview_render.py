"""Shared supersampling (SSAA) helpers for OpenSCAD preview renders.

OpenSCAD's OpenGL ``--preview`` applies little to no antialiasing, so an
image rendered at the requested resolution has visibly jagged, soft
edges — the "pixely" look. Rendering at an integer multiple of the
target size and downscaling with a Lanczos filter averages several
source pixels into each output pixel, producing crisp, anti-aliased
previews.

Every OpenSCAD preview surface in Kiln routes through these two helpers
so a single knob governs them all:

- ``model_visualizer`` — the universal multi-angle "show me this model"
- ``decoration_helpers`` — post-flip engraving previews
- ``multicolor_3mf`` — printer-LCD plate thumbnails
- ``generation.openscad`` — the compile-a-SCAD generation preview

Kiln's pure-Python ``colored_renderer`` already supersamples at a
matching 2× default (``render_colored_mesh(..., supersample=2)``); this
module brings the OpenSCAD-backed renders to the same quality bar.

Cost scales with the square of the factor in *render pixels*, but real
wall-clock grows far less because OpenSCAD's geometry compile is fixed
and only rasterization scales — the default 2× measured ~+40% per view.
Tune with ``KILN_PREVIEW_SUPERSAMPLE`` (1 disables; capped at 4).
"""

from __future__ import annotations

import contextlib
import logging
import os

logger = logging.getLogger(__name__)

PREVIEW_SUPERSAMPLE_DEFAULT = 2
PREVIEW_SUPERSAMPLE_MAX = 4
_ENV_VAR = "KILN_PREVIEW_SUPERSAMPLE"


def preview_supersample() -> int:
    """Return the preview supersampling factor (clamped to ``1..MAX``).

    Reads ``KILN_PREVIEW_SUPERSAMPLE`` when set; otherwise the default.
    A factor of 1 disables supersampling (native-resolution render).
    An unparseable value logs a warning and falls back to the default.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None or raw == "":
        return PREVIEW_SUPERSAMPLE_DEFAULT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r (want an integer); using %d",
            _ENV_VAR, raw, PREVIEW_SUPERSAMPLE_DEFAULT,
        )
        return PREVIEW_SUPERSAMPLE_DEFAULT
    return max(1, min(PREVIEW_SUPERSAMPLE_MAX, val))


def effective_supersample() -> int:
    """Like :func:`preview_supersample`, but degrades to 1 when Pillow is
    unavailable — a downscale is impossible without it, so rendering
    oversized would return a wrong-sized image. Callers that will call
    :func:`downscale_png` should size their render with this so the
    output always honors the requested dimensions.
    """
    ss = preview_supersample()
    if ss > 1:
        try:
            import PIL  # noqa: F401
        except ImportError:
            logger.debug("Pillow unavailable — rendering previews at native resolution")
            return 1
    return ss


def downscale_png(path: str, target_w: int, target_h: int) -> bool:
    """Downscale the PNG at *path* in place to *target_w*×*target_h*.

    Uses a Lanczos filter (the quality standard for downsampling). Writes
    to a sibling temp file and atomically replaces the original, so a
    failure mid-write never leaves a truncated preview. Returns ``True``
    on success; on any failure the original file is left untouched and
    ``False`` is returned so the caller can decide how to degrade.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as img:
            resample = getattr(Image, "Resampling", Image).LANCZOS
            resized = img.resize((target_w, target_h), resample)
        tmp = f"{path}.ss.tmp"
        resized.save(tmp, "PNG")
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001 — never let a preview crash the render
        logger.warning("Preview downscale failed for %s: %s", path, exc)
        with contextlib.suppress(OSError):
            os.unlink(f"{path}.ss.tmp")
        return False
