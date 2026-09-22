"""Heuristic snapshot analysis for webcam-based print monitoring.

Provides lightweight image validation using only stdlib — no PIL or
OpenCV dependency.  Detects obvious problems like corrupted images,
blocked cameras, cameras that are off, and images too poor for
meaningful print monitoring.

For actual print-defect detection (spaghetti, layer shifts, warping),
the agent should use a vision model on the base64-encoded image.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_SNAPSHOT_BYTES: int = 100
"""Minimum byte count for a valid image — anything smaller is corrupt."""

_JPEG_MAGIC: bytes = b"\xff\xd8\xff"
"""First three bytes of a valid JPEG file."""

_PNG_MAGIC: bytes = b"\x89PNG\r\n\x1a\n"
"""First eight bytes of a valid PNG file."""

_MIN_BRIGHTNESS: float = 0.05
"""Below this, the camera is likely off or the lens is fully blocked."""

_MAX_BRIGHTNESS: float = 0.98
"""Above this, the image is likely washed-out or overexposed."""

_MIN_VARIANCE: float = 0.01
"""Below this, the image is essentially uniform (all one colour)."""

_MIN_USABLE_DIMENSION: int = 320
"""Minimum width or height (in pixels) for a usable monitoring frame."""

_SAMPLE_SIZE: int = 1024
"""Number of evenly-spaced bytes to sample for brightness/variance."""

_MAX_VARIANCE_NORMALIZATION: float = 16256.0
"""Max raw variance for a uniform 0..255 distribution, used to normalise."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class SnapshotAnalysis:
    """Result of heuristic snapshot analysis.

    :param valid: Image is a recognised JPEG/PNG with enough data.
    :param brightness: 0.0–1.0 estimate; low means camera off/blocked.
    :param variance: 0.0–1.0 estimate; low means uniform image.
    :param size_bytes: Raw byte count of the image data.
    :param bed_visible: Whether the print bed is likely visible (heuristic).
    :param usable_quality: Whether the image meets minimum quality for monitoring.
    :param warnings: Human-readable issues detected.
    """

    valid: bool = False
    brightness: float = 0.0
    variance: float = 0.0
    size_bytes: int = 0
    bed_visible: bool = False
    usable_quality: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON responses."""
        return {
            "valid": self.valid,
            "brightness": self.brightness,
            "variance": self.variance,
            "size_bytes": self.size_bytes,
            "bed_visible": self.bed_visible,
            "usable_quality": self.usable_quality,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _estimate_brightness_and_variance(
    image_data: bytes,
    *,
    is_png: bool,
) -> tuple[float, float]:
    """Sample evenly-spaced bytes from the payload to estimate brightness and variance.

    This is not pixel-accurate — it operates on compressed data — but it is
    sufficient for detecting all-black, all-white, or uniform images without
    any image-processing dependency.
    """
    offset = 8 if is_png else 20  # skip format headers
    sample = image_data[offset:]

    if len(sample) < 64:
        return 0.0, 0.0

    step = max(1, len(sample) // _SAMPLE_SIZE)
    sampled = sample[::step][:_SAMPLE_SIZE]

    if len(sampled) == 0:
        return 0.0, 0.0

    total = 0
    sq_total = 0
    count = len(sampled)
    for b in sampled:
        total += b
        sq_total += b * b

    mean = total / count
    brightness = mean / 255.0
    variance_raw = (sq_total / count) - (mean * mean)
    variance = max(0.0, variance_raw) / _MAX_VARIANCE_NORMALIZATION

    return round(brightness, 4), round(variance, 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def image_dimensions(image_data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` for a PNG or JPEG, or ``None`` when unreadable.

    Stdlib only, like the rest of this module: PNG reads its IHDR, JPEG
    walks segment headers to the first start-of-frame.  Exists because
    :func:`analyze_snapshot` takes *width* and *height* as optional
    arguments and its usable-dimension check is skipped when they are
    absent -- so a caller that wants that check has to measure the frame,
    and every caller measuring it its own way is how they drift.
    """
    try:
        if image_data[:8] == _PNG_MAGIC:
            width, height = struct.unpack(">II", image_data[16:24])
            return (int(width), int(height))
        if image_data[:3] == _JPEG_MAGIC:
            i = 2
            end = len(image_data)
            while i + 9 < end:
                if image_data[i] != 0xFF:
                    i += 1
                    continue
                marker = image_data[i + 1]
                # Start-of-frame markers carry the dimensions; C4/C8/CC are
                # tables and extensions that merely look like them.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", image_data[i + 5:i + 9])
                    return (int(width), int(height))
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                segment = struct.unpack(">H", image_data[i + 2:i + 4])[0]
                i += 2 + segment
    except Exception:  # noqa: BLE001 -- an unreadable header is "no dimensions"
        logger.debug("image dimensions not readable", exc_info=True)
    return None


def analyze_snapshot(
    image_data: bytes,
    *,
    previous_brightness: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> SnapshotAnalysis:
    """Run heuristic checks on a webcam snapshot for print monitoring.

    Uses only stdlib — no PIL or OpenCV dependency.  Designed for FDM print
    monitoring: checks whether the camera feed is usable, the bed is likely
    visible, and there is frame-to-frame change (indicating movement).

    :param image_data: Raw image bytes (JPEG or PNG).
    :param previous_brightness: Brightness of the prior frame, if available.
        Used to detect frame-to-frame change (movement on the bed).
    :param width: Known image width in pixels, if available.
    :param height: Known image height in pixels, if available.
    :returns: :class:`SnapshotAnalysis` with validation results and warnings.
    """
    warnings: list[str] = []
    size_bytes = len(image_data) if image_data else 0

    # --- Size check ---
    if size_bytes < _MIN_SNAPSHOT_BYTES:
        return SnapshotAnalysis(
            size_bytes=size_bytes,
            warnings=["image too small — likely corrupt or empty"],
        )

    # --- Format validation ---
    is_jpeg = image_data[:3] == _JPEG_MAGIC
    is_png = image_data[:8] == _PNG_MAGIC

    if not is_jpeg and not is_png:
        return SnapshotAnalysis(
            size_bytes=size_bytes,
            warnings=["unrecognised image format — expected JPEG or PNG"],
        )

    # --- Brightness and variance ---
    brightness, variance = _estimate_brightness_and_variance(
        image_data,
        is_png=is_png,
    )

    if brightness == 0.0 and variance == 0.0 and size_bytes > _MIN_SNAPSHOT_BYTES:
        warnings.append("image too small to estimate brightness reliably")

    # --- FDM-specific: bed visibility (brightness check) ---
    bed_visible = True

    if brightness < _MIN_BRIGHTNESS:
        warnings.append("too dark — camera may be off or lens blocked")
        bed_visible = False

    if brightness > _MAX_BRIGHTNESS:
        warnings.append("overexposed — image is nearly all white")
        bed_visible = False

    if variance < _MIN_VARIANCE:
        warnings.append("very low variance — image may be uniform (camera off or blocked)")
        bed_visible = False

    # --- FDM-specific: movement detection via frame delta ---
    if previous_brightness is not None:
        delta = abs(brightness - previous_brightness)
        if delta < 0.002:
            warnings.append("no brightness change between frames — print may be stalled or camera frozen")

    # --- Resolution / usable quality check ---
    usable_quality = True

    if width is not None and width < _MIN_USABLE_DIMENSION:
        warnings.append(f"image width {width}px below minimum {_MIN_USABLE_DIMENSION}px")
        usable_quality = False

    if height is not None and height < _MIN_USABLE_DIMENSION:
        warnings.append(f"image height {height}px below minimum {_MIN_USABLE_DIMENSION}px")
        usable_quality = False

    # A valid image that is too dark or uniform is still not usable
    if not bed_visible:
        usable_quality = False

    return SnapshotAnalysis(
        valid=True,
        brightness=brightness,
        variance=variance,
        size_bytes=size_bytes,
        bed_visible=bed_visible,
        usable_quality=usable_quality,
        warnings=warnings,
    )
