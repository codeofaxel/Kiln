"""Convert raster images to surface heightmaps for OpenSCAD embossing/debossing.

Supports PNG/JPG to PGM conversion, SVG passthrough validation,
text metadata for OpenSCAD native text(), and QR code generation.

PGM P5 (binary Portable Gray Map) is the format OpenSCAD's surface() reads.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import struct
import subprocess
import zlib
from pathlib import Path

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PNG pure-Python decoder (RGB, RGBA, Gray, Gray+Alpha, filter types 0-4)
# ---------------------------------------------------------------------------

def _read_png_pixels(file_path: str) -> tuple[list[list[int]], int, int]:
    """Read a PNG file and return (rows_of_grayscale, width, height).

    Each row is a list of ints 0-255 representing grayscale intensity.
    Transparency is mapped to 255 (white = no emboss).
    """
    with open(file_path, "rb") as f:
        data = f.read()

    # Verify PNG signature
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG file: {file_path}")

    # Parse chunks
    pos = 8
    ihdr = None
    idat_chunks: list[bytes] = []

    while pos < len(data):
        chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + chunk_len]
        # skip CRC (4 bytes after chunk data)
        pos += 12 + chunk_len

        if chunk_type == b"IHDR":
            width = struct.unpack(">I", chunk_data[0:4])[0]
            height = struct.unpack(">I", chunk_data[4:8])[0]
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
            ihdr = {
                "width": width,
                "height": height,
                "bit_depth": bit_depth,
                "color_type": color_type,
            }
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if ihdr is None:
        raise ValueError("PNG missing IHDR chunk")

    width = ihdr["width"]
    height = ihdr["height"]
    color_type = ihdr["color_type"]
    bit_depth = ihdr["bit_depth"]

    if bit_depth != 8:
        raise ValueError(f"Only 8-bit PNGs supported, got {bit_depth}-bit")

    # Bytes per pixel
    bpp_map = {0: 1, 2: 3, 4: 2, 6: 4}  # Gray, RGB, Gray+A, RGBA
    if color_type not in bpp_map:
        raise ValueError(f"Unsupported PNG color type: {color_type}")
    bpp = bpp_map[color_type]

    # Decompress all IDAT data
    raw = zlib.decompress(b"".join(idat_chunks))

    stride = width * bpp + 1  # +1 for filter byte per row

    def _paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        if pb <= pc:
            return b
        return c

    # Reconstruct filtered rows
    prev_row = bytes(width * bpp)
    rows: list[list[int]] = []

    for y in range(height):
        row_start = y * stride
        filter_type = raw[row_start]
        raw_row = bytearray(raw[row_start + 1 : row_start + 1 + width * bpp])

        for i in range(len(raw_row)):
            a = raw_row[i - bpp] if i >= bpp else 0
            b = prev_row[i]
            c = prev_row[i - bpp] if i >= bpp else 0

            if filter_type == 0:  # None
                pass
            elif filter_type == 1:  # Sub
                raw_row[i] = (raw_row[i] + a) & 0xFF
            elif filter_type == 2:  # Up
                raw_row[i] = (raw_row[i] + b) & 0xFF
            elif filter_type == 3:  # Average
                raw_row[i] = (raw_row[i] + (a + b) // 2) & 0xFF
            elif filter_type == 4:  # Paeth
                raw_row[i] = (raw_row[i] + _paeth(a, b, c)) & 0xFF
            else:
                raise ValueError(f"Unknown PNG filter type: {filter_type}")

        prev_row = bytes(raw_row)

        # Convert to grayscale row
        gray_row: list[int] = []
        if color_type == 0:  # Grayscale
            gray_row = list(raw_row)
        elif color_type == 2:  # RGB
            for x in range(width):
                off = x * 3
                r, g, b_val = raw_row[off], raw_row[off + 1], raw_row[off + 2]
                gray_row.append(int(0.299 * r + 0.587 * g + 0.114 * b_val))
        elif color_type == 4:  # Grayscale + Alpha
            for x in range(width):
                off = x * 2
                gray, alpha = raw_row[off], raw_row[off + 1]
                # Blend against white background for transparent pixels
                blended = int(gray * (alpha / 255.0) + 255 * (1 - alpha / 255.0))
                gray_row.append(min(255, blended))
        elif color_type == 6:  # RGBA
            for x in range(width):
                off = x * 4
                r, g, b_val, alpha = (
                    raw_row[off],
                    raw_row[off + 1],
                    raw_row[off + 2],
                    raw_row[off + 3],
                )
                gray = int(0.299 * r + 0.587 * g + 0.114 * b_val)
                blended = int(gray * (alpha / 255.0) + 255 * (1 - alpha / 255.0))
                gray_row.append(min(255, blended))

        rows.append(gray_row)

    return rows, width, height


# ---------------------------------------------------------------------------
# Image loading: Pillow first, then pure-Python PNG, then sips/convert for JPEG
# ---------------------------------------------------------------------------

def _load_image_as_grayscale(image_path: str) -> tuple[list[list[int]], int, int]:
    """Load an image and return (rows_of_grayscale, width, height).

    Attempts in order:
    1. Pillow (PIL) -- handles all formats
    2. Pure-Python PNG decoder (PNG only)
    3. macOS sips or ImageMagick convert to transcode JPEG->PNG, then decode

    Returns rows as list of lists of int (0-255 grayscale).
    """
    abs_path = os.path.abspath(image_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Image not found: {abs_path}")

    ext = os.path.splitext(abs_path)[1].lower()

    # Strategy 1: Try Pillow
    try:
        from PIL import Image  # type: ignore[import-untyped]

        img = Image.open(abs_path)
        if img.mode == "RGBA" or img.mode == "PA":
            # Composite onto white background
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        img = img.convert("L")
        w, h = img.size
        pixels = list(img.getdata())
        rows = [pixels[i * w : (i + 1) * w] for i in range(h)]
        return rows, w, h
    except ImportError:
        pass

    # Strategy 2: Pure-Python PNG
    if ext == ".png":
        return _read_png_pixels(abs_path)

    # Strategy 3: Convert JPEG to PNG via system tools, then decode PNG
    if ext in (".jpg", ".jpeg"):
        import tempfile

        fd, tmp_png = tempfile.mkstemp(suffix=".png", prefix="kiln_jpeg_")
        os.close(fd)

        try:
            # Try macOS sips
            try:
                subprocess.run(
                    ["sips", "-s", "format", "png", abs_path, "--out", tmp_png],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                result = _read_png_pixels(tmp_png)
                return result
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

            # Try ImageMagick convert
            try:
                subprocess.run(
                    ["convert", abs_path, tmp_png],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                result = _read_png_pixels(tmp_png)
                return result
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

            raise RuntimeError(
                "Cannot decode JPEG without Pillow, macOS sips, or ImageMagick. "
                "Install Pillow: pip install Pillow"
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_png)

    raise ValueError(f"Unsupported image format: {ext}")


# ---------------------------------------------------------------------------
# Image processing helpers
# ---------------------------------------------------------------------------

def _downscale(
    rows: list[list[int]], src_w: int, src_h: int, max_res: int
) -> tuple[list[list[int]], int, int]:
    """Downscale using area averaging so the longest edge is <= max_res."""
    if max(src_w, src_h) <= max_res:
        return rows, src_w, src_h

    scale = max_res / max(src_w, src_h)
    dst_w = max(1, int(src_w * scale))
    dst_h = max(1, int(src_h * scale))

    out: list[list[int]] = []
    for dy in range(dst_h):
        row: list[int] = []
        # Source y range for this output row
        sy0 = dy * src_h / dst_h
        sy1 = (dy + 1) * src_h / dst_h
        for dx in range(dst_w):
            sx0 = dx * src_w / dst_w
            sx1 = (dx + 1) * src_w / dst_w

            # Area-average all source pixels that fall in this block
            total = 0.0
            count = 0.0
            for sy in range(int(sy0), min(int(sy1) + 1, src_h)):
                # Vertical coverage fraction
                vy = min(sy + 1, sy1) - max(sy, sy0)
                if vy <= 0:
                    continue
                for sx in range(int(sx0), min(int(sx1) + 1, src_w)):
                    vx = min(sx + 1, sx1) - max(sx, sx0)
                    if vx <= 0:
                        continue
                    weight = vx * vy
                    total += rows[sy][sx] * weight
                    count += weight

            row.append(int(total / count) if count > 0 else 0)
        out.append(row)

    return out, dst_w, dst_h


def _sharpen(rows: list[list[int]], w: int, h: int) -> list[list[int]]:
    """Apply a 3x3 sharpening kernel: center=5, edges=-1, corners=0."""
    out: list[list[int]] = []
    for y in range(h):
        row: list[int] = []
        for x in range(w):
            center = rows[y][x] * 5
            neighbors = 0
            if y > 0:
                neighbors += rows[y - 1][x]
            if y < h - 1:
                neighbors += rows[y + 1][x]
            if x > 0:
                neighbors += rows[y][x - 1]
            if x < w - 1:
                neighbors += rows[y][x + 1]
            val = center - neighbors
            row.append(max(0, min(255, val)))
        out.append(row)
    return out


def _invert(rows: list[list[int]]) -> list[list[int]]:
    """Flip light/dark values."""
    return [[255 - v for v in row] for row in rows]


def _write_pgm(path: str, rows: list[list[int]], w: int, h: int) -> None:
    """Write a PGM P5 (binary) file."""
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        for row in rows:
            f.write(bytes(row))


def _mask_rows(
    rows: list[list[int]], w: int, h: int, mask: str, style: str
) -> list[list[int]]:
    """Confine a photo heightmap to a product-matched pool, in rows space.

    Pure Python on the row grid — no imaging dependency can be missing, so
    this boundary cannot be skipped the way the in-style mask was when
    rembg was absent.  Outside the shape maps to 0 (no displacement); the
    shape is inset from the grid edge so the outermost ring never carves.
    """
    effective = mask if mask != "auto" else ("circle" if style == "coin" else "rounded_rectangle")
    inset = max(2.0, min(w, h) * 0.02)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    if effective == "circle":
        r = min(cx, cy) - inset

        def _inside(x: float, y: float) -> bool:
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    else:  # rectangle / rounded_rectangle — softened corners either way
        corner = min(w, h) * 0.08
        x0, y0 = inset, inset
        x1, y1 = w - 1 - inset, h - 1 - inset

        def _inside(x: float, y: float) -> bool:
            if x < x0 or x > x1 or y < y0 or y > y1:
                return False
            dx = max(x0 + corner - x, x - (x1 - corner), 0.0)
            dy = max(y0 + corner - y, y - (y1 - corner), 0.0)
            return dx * dx + dy * dy <= corner * corner

    return [
        [v if _inside(x, y) else 0 for x, v in enumerate(row)]
        for y, row in enumerate(rows)
    ]


def _is_mark_on_flat_field(rows: list[list[int]], w: int, h: int) -> bool:
    """True when the image is artwork on a clean field (a logo), not a photo.

    A logo carries its meaning in the MARK; the field around it is empty
    space that should stay flush with the part. A photo carries meaning
    everywhere, so its field is content and a coin-style carve is right.
    Telling them apart is what stops a logo from being sunk into a
    rectangular (or rounded, or circular) tray of its own background.

    The test is deliberately conservative — a uniform border AND a
    strongly two-tone histogram AND sparse ink — so a photo is never
    mistaken for a mark.
    """
    if w < 8 or h < 8:
        return False
    border = rows[0] + rows[-1] + [r[0] for r in rows] + [r[-1] for r in rows]
    lo, hi = min(border), max(border)
    if hi - lo > 12:
        return False  # a photo's edge is not one flat tone
    field = (lo + hi) // 2
    flat = sum(1 for r in rows for v in r if abs(v - field) <= 12)
    total = w * h
    if flat / total < 0.45:
        return False  # too little empty space to be a mark on a field
    # Bimodal: most non-field pixels sit far from the field tone.
    far = sum(1 for r in rows for v in r if abs(v - field) > 60)
    non_field = total - flat
    return non_field > 0 and far / max(1, non_field) > 0.5


def _flatten_field(rows: list[list[int]], w: int, h: int) -> list[list[int]]:
    """Map a mark's background to the no-carve level, artwork to full carve.

    After this the heightmap carves the MARK and nothing else, so no
    boundary of any shape can appear on the part.
    """
    border = rows[0] + rows[-1] + [r[0] for r in rows] + [r[-1] for r in rows]
    field = (min(border) + max(border)) // 2
    span = max(1, max(abs(255 - field), abs(field)))
    # Deadband: JPEG noise and posterizing leave the field a tone or two off
    # true flat, which would carve a few microns everywhere — invisible in
    # the data, but it is still a cut where the part should be untouched.
    # Anything within this band of the field tone is exactly flush.
    deadband = 16

    def _v(value: int) -> int:
        delta = abs(value - field)
        if delta <= deadband:
            return 0
        return min(255, int((delta - deadband) * 255 / max(1, span - deadband)))

    return [[_v(v) for v in row] for row in rows]


def _shape_mask_image(img, mask: str, style: str):
    """Confine an emboss image to a product-shaped area, zeroing outside.

    The area outside the mask is set to 0 — the no-carve level — so the
    carve boundary is a deliberate shape (a circle on a round product, a
    softened rectangle otherwise) instead of the source image's raw
    rectangle.  A raw rectangle reads on the printed part as a sunken
    photo frame around the artwork, which is never what anyone asked for.

    Kept module-level and dependency-free (Pillow only) so EVERY path can
    call it: the full coin pipeline, and the degraded path taken when an
    optional dependency such as rembg is missing.  Previously the mask
    lived inside the coin block, so a missing rembg skipped masking
    entirely and shipped the frame (2026-07-29).
    """
    from PIL import Image, ImageDraw

    effective = mask if mask != "auto" else ("circle" if style == "coin" else "rounded_rectangle")
    if effective == "rectangle":
        # Explicit full-bleed: still soften the corners so the boundary is
        # not a hard photographic rectangle.
        effective = "rounded_rectangle"

    # Inset by at least a pixel: a shape drawn flush to the bitmap edge
    # leaves the outermost ring INSIDE it, so the field there still carves
    # and the frame survives with rounded corners.
    inset = max(2, int(min(img.size) * 0.02))
    shape = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(shape)
    if effective == "circle":
        cx, cy = img.size[0] // 2, img.size[1] // 2
        r = min(cx, cy) - inset
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    else:
        corner_r = int(min(img.size) * 0.08)
        draw.rounded_rectangle(
            [inset, inset, img.size[0] - 1 - inset, img.size[1] - 1 - inset],
            radius=corner_r, fill=255,
        )
    out = Image.new("L", img.size, 0)
    out.paste(img, mask=shape)
    return out


def _write_dat(path: str, rows: list[list[int]], w: int, h: int) -> None:
    """Write a DAT heightmap file for OpenSCAD ``surface()``.

    OpenSCAD's ``surface()`` reads text-based DAT files: one row per
    line, space-separated float values.  Values represent height — 0 is
    flat, higher values are taller.  We normalise to 0.0–1.0 range so
    the ``scale()`` in OpenSCAD controls the actual depth.
    """
    with open(path, "w", encoding="ascii") as f:
        for row in rows:
            f.write(" ".join(f"{v / 255.0:.4f}" for v in row) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# HEIC / HEIF container magic — iPhone default format, frequently carries
# a .jpg extension after AirDrop/Photos export.  PIL cannot read these; we
# detect by ISO-BMFF magic bytes and auto-convert via macOS `sips`.
_HEIC_BRANDS = frozenset({
    b"heic", b"heix", b"hevc", b"hevx", b"mif1",
    b"msf1", b"heim", b"heis", b"avif", b"avis",
})


def _is_heic_container(path: str) -> bool:
    """Detect HEIC/HEIF by ISO-BMFF magic bytes, regardless of extension."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS


def _auto_convert_heic(src: str, work_dir: str) -> str:
    """Convert HEIC → JPEG via macOS sips.  Raises RuntimeError on failure.

    This exists so iPhone photos (which default to HEIC even when saved
    with a .jpg extension) don't silently fail downstream in PIL.
    """
    import shutil as _shutil
    import subprocess as _sp
    if not _shutil.which("sips"):
        raise RuntimeError(
            f"{os.path.basename(src)} is HEIC/HEIF (iPhone format).  "
            "PIL cannot read this directly.  On macOS this is auto-converted "
            "via `sips`, but `sips` was not found on PATH.  Convert the file "
            "manually (Preview → Export → JPEG) and retry."
        )
    out = os.path.join(work_dir, f"heic_converted_{os.path.basename(src)}.jpg")
    try:
        _sp.run(
            ["sips", "-s", "format", "jpeg", src, "--out", out],
            check=True, capture_output=True, timeout=30,
        )
    except (_sp.CalledProcessError, _sp.TimeoutExpired) as exc:
        raise RuntimeError(
            f"HEIC→JPEG conversion failed for {os.path.basename(src)}: {exc}.  "
            "Convert manually and retry."
        ) from exc
    return out


def prepare_image_for_emboss(
    image_path: str,
    output_dir: str,
    *,
    max_resolution: int = 200,
    invert: bool = False,
    edge_enhance: bool = True,
    style: str = "default",
    flip_rows: bool = False,
    mask: str = "auto",
) -> dict:
    """Convert a PNG/JPG image to a grayscale PGM heightmap for OpenSCAD surface().

    Parameters
    ----------
    image_path : str
        Path to input PNG or JPEG image.
    output_dir : str
        Directory where the output PGM file will be written.
    max_resolution : int
        Maximum pixel count on the longest edge (default 200).
    invert : bool
        If True, flip light/dark (switch between deboss and emboss).
    edge_enhance : bool
        If True, apply a sharpening kernel for crisper edges.
    mask : str
        Final crop mask shape. ``"auto"`` (default) uses the style's
        native mask (circle for coin, none for others).  ``"circle"``
        forces circular medallion framing.  ``"rectangle"`` skips
        masking entirely — full rectangular image.  ``"rounded_rectangle"``
        applies soft rounded corners (corner_radius = 8% of size).

    Returns
    -------
    dict
        Keys: pgm_path, width_px, height_px, aspect_ratio
    """
    # Pre-flight: detect HEIC (iPhone default, commonly mislabeled .jpg) and
    # auto-convert before PIL gets a chance to choke silently.  This is the
    # earliest point the image path flows into the kiln decoration pipeline,
    # so handling it here covers decorate_surface AND all product generators.
    if _is_heic_container(image_path):
        os.makedirs(output_dir, exist_ok=True)
        image_path = _auto_convert_heic(image_path, output_dir)

    # ---- Content decides the treatment, before any style runs ----
    # A logo/glyph is artwork on empty space: carve the MARK, leave the
    # field flush.  A photo is content everywhere: coin relief inside a
    # product-shaped mask.  Deciding here, on the SOURCE image, is what
    # makes the difference structural — the photo pipeline masks to a
    # circle, and running it over a logo carved the whole disc away and
    # left the mask's edge printed on the part as a ring.  Callers pass a
    # style as a hint; the image itself is the authority.
    _mark_mode = False
    try:
        _probe_rows, _pw, _ph = _load_image_as_grayscale(image_path)
        _probe_rows, _pw, _ph = _downscale(_probe_rows, _pw, _ph, max_resolution)
        _mark_mode = _is_mark_on_flat_field(_probe_rows, _pw, _ph)
    except Exception:  # noqa: BLE001 — a probe failure just means "treat as photo"
        _mark_mode = False

    if _mark_mode:
        _logger.info(
            "Emboss: artwork on a clean field detected — carving the mark "
            "only, leaving the surrounding surface flush."
        )
        style = "default"
        edge_enhance = False

    # Apply style-specific preprocessing
    if style == "photo":
        # Best for photos of people, pets, objects.
        # High contrast + gaussian blur + 3-level posterize.
        # Produces clean depth tiers that FDM printers resolve well.
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            img = Image.open(image_path).convert('L')
            # Resize to target resolution first
            # (handled below, so we process at original res)
            img = ImageEnhance.Contrast(img).enhance(2.5)
            img = ImageEnhance.Brightness(img).enhance(1.1)
            img = img.filter(ImageFilter.GaussianBlur(radius=2))
            # 3-level posterize: dark=0, mid=128, light=255
            img = img.point(lambda p: 0 if p < 80 else (128 if p < 170 else 255))
            # Save preprocessed and use as input
            preprocessed = os.path.join(output_dir, "preprocessed_photo.png")
            os.makedirs(output_dir, exist_ok=True)
            img.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False  # don't edge enhance posterized images
        except ImportError:
            _logger.warning(
                "Style 'photo' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "stencil":
        # Bold binary silhouette — high contrast, two levels only.
        # Good for simple subjects against clean backgrounds.
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            img = Image.open(image_path).convert('L')
            img = ImageEnhance.Contrast(img).enhance(2.0)
            img = img.point(lambda p: 255 if p > 140 else 0)
            img = img.filter(ImageFilter.MedianFilter(size=5))
            preprocessed = os.path.join(output_dir, "preprocessed_stencil.png")
            os.makedirs(output_dir, exist_ok=True)
            img.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
        except ImportError:
            _logger.warning(
                "Style 'stencil' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "lithophane":
        # Full grayscale gradient — for backlit/translucent prints.
        # No posterization, maximum tonal range.
        # Inverts by default (thin = bright when backlit).
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            img = Image.open(image_path).convert('L')
            img = ImageEnhance.Contrast(img).enhance(1.5)
            img = img.filter(ImageFilter.GaussianBlur(radius=1))
            preprocessed = os.path.join(output_dir, "preprocessed_lithophane.png")
            os.makedirs(output_dir, exist_ok=True)
            img.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
            invert = True  # lithophanes: thin = bright
        except ImportError:
            _logger.warning(
                "Style 'lithophane' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "coin":
        # PROVEN pipeline (v11 Ash coaster, 2026-04-02):
        # rembg → dodge+burn → bilateral → 8-level posterize → circular mask.
        # Handles dark subjects (dark fur, dark clothing) via local contrast
        # normalization (dodge+burn) instead of histogram equalization.
        # Recommended: max_resolution=250, depth=1.5-2.0mm, white PLA.
        try:
            from PIL import Image, ImageFilter, ImageOps

            img = ImageOps.exif_transpose(Image.open(image_path)).convert("L")

            # Step 1: EXIF-aware open + transpose
            # Step 2: Background removal via rembg (if available)
            try:
                from rembg import remove as _rembg_remove

                img_rgba = ImageOps.exif_transpose(Image.open(image_path))
                img_rgba = _rembg_remove(img_rgba)
                # Convert removed background (transparent) to white
                bg = Image.new("RGBA", img_rgba.size, (255, 255, 255, 255))
                bg.paste(img_rgba, mask=img_rgba.split()[3])
                img = bg.convert("L")
            except ImportError:
                # rembg is an ENHANCEMENT (subject isolation), not a
                # prerequisite.  Raising here aborted the whole coin
                # pipeline — including the shape mask — and the fallback
                # carved the source image's raw rectangle into the part as
                # a sunken photo frame.  Degrade to no background removal
                # and keep every other step, mask included.
                _logger.info(
                    "Photo emboss: background removal unavailable "
                    "(pip install 'kiln3d[emboss]') — continuing without it; "
                    "the subject may not separate as cleanly."
                )

            # Step 3: Foreground crop with 8% padding
            bbox = img.getbbox()
            if bbox:
                pad_x = int((bbox[2] - bbox[0]) * 0.08)
                pad_y = int((bbox[3] - bbox[1]) * 0.08)
                crop_box = (
                    max(0, bbox[0] - pad_x),
                    max(0, bbox[1] - pad_y),
                    min(img.width, bbox[2] + pad_x),
                    min(img.height, bbox[3] + pad_y),
                )
                img = img.crop(crop_box)

            # Resize to target resolution
            # Circle mask needs square; rectangle/rounded_rectangle preserve aspect
            effective_mask = mask if mask != "auto" else "circle"
            if effective_mask == "circle":
                img = ImageOps.fit(img, (max_resolution, max_resolution), method=Image.LANCZOS)
            else:
                img.thumbnail((max_resolution, max_resolution), Image.LANCZOS)

            # Step 4: Dodge+burn — local contrast normalization
            # pixel / local_average via GaussianBlur radius=22
            # Uses numpy when available (best quality), falls back to
            # histogram equalization otherwise.
            try:
                import numpy as np

                arr = np.array(img, dtype=np.float64)
                local_avg = np.array(
                    img.filter(ImageFilter.GaussianBlur(radius=22)),
                    dtype=np.float64,
                )
                # Avoid division by zero
                local_avg = np.clip(local_avg, 1.0, 255.0)
                dodged = arr / local_avg * 128.0
                dodged = np.clip(dodged, 0, 255).astype(np.uint8)
                img = Image.fromarray(dodged)
            except ImportError:
                # Fallback: equalize + autocontrast (less good for dark
                # subjects but works without numpy)
                img = ImageOps.equalize(img)
                img = ImageOps.autocontrast(img, cutoff=2)

            # Step 5: Bilateral smoothing — MedianFilter(3) x3
            for _ in range(3):
                img = img.filter(ImageFilter.MedianFilter(size=3))

            # Step 6: Adaptive posterize — detect contrast and adjust
            # levels for clean FDM depth tiers.
            #
            # High-contrast subjects (dark dog on light background, like
            # Fig 2 close-up face) work great with 8 levels — the tonal
            # range is already spread and 8 steps resolve fine detail.
            #
            # Low-contrast subjects (cream dog in pink hood on white
            # blanket, like Fig 1) have most pixels clustered in a narrow
            # brightness band.  8 levels = most steps are near-identical
            # height → flat-looking deboss.  Fewer levels (5-6) force
            # bigger height jumps per step → more visible coin relief.
            #
            # Heuristic: compute the interquartile range (IQR) of pixel
            # values after dodge+burn.  If IQR < 80 (compressed tonal
            # range), drop to 5 posterize levels.  Otherwise use 8.
            try:
                import numpy as _np_contrast
                _arr = _np_contrast.array(img)
                _q25, _q75 = _np_contrast.percentile(_arr, [25, 75])
                _iqr = _q75 - _q25
                if _iqr < 80:
                    _posterize_levels = 5
                    _logger.info(
                        "Low-contrast image detected (IQR=%.0f < 80). "
                        "Using %d posterize levels for stronger coin relief.",
                        _iqr, _posterize_levels,
                    )
                else:
                    _posterize_levels = 8
            except ImportError:
                _posterize_levels = 8

            step = 256 // _posterize_levels
            img = img.point(
                lambda x: (x // step) * step * 255 // (step * (_posterize_levels - 1))
            )

            # Confine the carve to a product shape — see _shape_mask_image.
            img = _shape_mask_image(img, mask, "coin")

            preprocessed = os.path.join(output_dir, "preprocessed_coin.png")
            os.makedirs(output_dir, exist_ok=True)
            img.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
        except ImportError:
            _logger.warning(
                "Style 'coin' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "portrait":
        # Edge-detected portrait: equalize + edge detection + dilation.
        # Best for: line-art style emboss, subjects with clear outlines.
        # Not ideal for dark-on-dark subjects (use "coin" instead).
        # Recommended depth: 0.8-1.5mm.
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageOps

            img = Image.open(image_path).convert("L")
            img = ImageOps.fit(img, (max_resolution, max_resolution), method=Image.LANCZOS)
            # Equalize to pull detail from dark areas
            eq = ImageOps.equalize(img)
            eq = ImageOps.autocontrast(eq, cutoff=3)
            smooth = eq.filter(ImageFilter.MedianFilter(size=3))
            smooth = smooth.filter(ImageFilter.GaussianBlur(radius=0.8))
            edges = smooth.filter(ImageFilter.FIND_EDGES)
            edges = edges.point(lambda x: min(255, x * 3))
            # Laplacian for finer detail
            lap = smooth.filter(
                ImageFilter.Kernel(
                    size=(3, 3),
                    kernel=[-1, -1, -1, -1, 8, -1, -1, -1, -1],
                    scale=1,
                    offset=0,
                )
            )
            lap = lap.point(lambda x: min(255, x * 2))
            edges = Image.blend(edges, lap, 0.4)
            edges = edges.point(lambda x: 255 if x > 40 else 0)
            # Dilate for minimum print width (~0.8mm)
            edges = edges.filter(ImageFilter.MaxFilter(size=3))
            edges = edges.filter(ImageFilter.GaussianBlur(radius=0.5))
            # Circular mask
            mask = Image.new("L", edges.size, 0)
            draw = ImageDraw.Draw(mask)
            cx, cy = edges.size[0] // 2, edges.size[1] // 2
            r = min(cx, cy) - 2
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
            result = Image.new("L", edges.size, 0)
            result.paste(edges, mask=mask)
            preprocessed = os.path.join(output_dir, "preprocessed_portrait.png")
            os.makedirs(output_dir, exist_ok=True)
            result.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
        except ImportError:
            _logger.warning(
                "Style 'portrait' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "composite":
        # Hybrid: posterized base (volume) + edge overlay (definition).
        # Combines the clean depth tiers of "coin" with edge sharpness.
        # Good all-rounder for photos with both broad areas and fine detail.
        # Recommended depth: 1.0-1.5mm.
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageOps

            img = Image.open(image_path).convert("L")
            img = ImageOps.fit(img, (max_resolution, max_resolution), method=Image.LANCZOS)
            # Base: posterized for volume
            eq = ImageOps.equalize(img)
            eq = ImageOps.autocontrast(eq, cutoff=2)
            step = 256 // 4
            base = eq.point(lambda x: (x // step) * step * 255 // (step * 3))
            # Overlay: edge detail
            smooth = img.filter(ImageFilter.MedianFilter(size=3))
            edge = smooth.filter(ImageFilter.FIND_EDGES)
            edge = edge.point(lambda x: min(255, x * 3))
            edge = edge.filter(ImageFilter.MaxFilter(size=3))
            edge = edge.filter(ImageFilter.GaussianBlur(radius=0.5))
            # Blend: 70% base + 30% edge
            comp = Image.blend(base, edge, 0.3)
            comp = ImageOps.autocontrast(comp, cutoff=1)
            # Circular mask
            mask = Image.new("L", comp.size, 0)
            draw = ImageDraw.Draw(mask)
            cx, cy = comp.size[0] // 2, comp.size[1] // 2
            r = min(cx, cy) - 2
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
            result = Image.new("L", comp.size, 0)
            result.paste(comp, mask=mask)
            preprocessed = os.path.join(output_dir, "preprocessed_composite.png")
            os.makedirs(output_dir, exist_ok=True)
            result.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
        except ImportError:
            _logger.warning(
                "Style 'composite' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    elif style == "medallion":
        # Coin with raised border ring — like a commemorative medal.
        # Border ring is full height, relief is 80% height, inner bevel.
        # Most premium single-color look. Recommended depth: 1.5-2.0mm.
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageOps

            img = Image.open(image_path).convert("L")
            sz = max_resolution
            img = ImageOps.fit(img, (sz, sz), method=Image.LANCZOS)
            # Relief: 5-level posterize + subtle edge enhancement
            eq = ImageOps.equalize(img)
            eq = ImageOps.autocontrast(eq, cutoff=2)
            step = 256 // 5
            relief = eq.point(lambda x: (x // step) * step * 255 // (step * 4))
            smooth = img.filter(ImageFilter.MedianFilter(size=3))
            edge = smooth.filter(ImageFilter.FIND_EDGES)
            edge = edge.point(lambda x: min(255, x * 2))
            edge = edge.filter(ImageFilter.MaxFilter(size=3))
            edge = edge.filter(ImageFilter.GaussianBlur(radius=0.5))
            relief = Image.blend(relief, edge, 0.25)
            relief = ImageOps.autocontrast(relief, cutoff=1)
            # Build medallion structure
            result = Image.new("L", (sz, sz), 0)
            draw = ImageDraw.Draw(result)
            cx, cy = sz // 2, sz // 2
            r_outer = sz // 2 - 2
            r_inner = sz // 2 - 8  # 8px border
            r_relief = r_inner - 3  # gap between border and relief
            # Outer border ring at full height
            draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=255)
            draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=0)
            # Relief inside border (scaled to 80% max height)
            relief_mask = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(relief_mask).ellipse(
                [cx - r_relief, cy - r_relief, cx + r_relief, cy + r_relief], fill=255
            )
            relief_scaled = relief.point(lambda x: int(x * 200 / 255))
            result.paste(relief_scaled, mask=relief_mask)
            # Inner bevel ring
            bevel = Image.new("L", (sz, sz), 0)
            bevel_draw = ImageDraw.Draw(bevel)
            bevel_draw.ellipse(
                [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=180
            )
            bevel_draw.ellipse(
                [cx - (r_inner - 2), cy - (r_inner - 2), cx + (r_inner - 2), cy + (r_inner - 2)],
                fill=0,
            )
            result = Image.composite(
                bevel, result, bevel.point(lambda x: 255 if x > 0 else 0)
            )
            preprocessed = os.path.join(output_dir, "preprocessed_medallion.png")
            os.makedirs(output_dir, exist_ok=True)
            result.save(preprocessed)
            image_path = preprocessed
            edge_enhance = False
        except ImportError:
            _logger.warning(
                "Style 'medallion' requires Pillow. Install with: pip install pillow. "
                "Falling back to default processing."
            )

    # style == "default" — no preprocessing, use existing behavior

    # Boundary guarantee, applied on EVERY path: an image decoration must
    # never carve its own outline into the part.  Two shapes of input, two
    # right answers, decided here rather than per-generator:
    #
    #   * a MARK on a clean field (logo, glyph, traced SVG) — carve the
    #     mark, leave the field flush.  Nothing but the artwork is cut, so
    #     no frame of any shape can appear.
    #   * a PHOTO — coin relief inside a product-shaped mask, inset far
    #     enough that the bitmap's own edge never carves.
    #
    # Before this, the shape mask lived inside the coin pipeline, that
    # pipeline aborted when the optional rembg package was missing, and the
    # fallback carved the source rectangle into the part as a sunken photo
    # frame (found on a real coaster, 2026-07-29).
    rows, w, h = _load_image_as_grayscale(image_path)
    rows, w, h = _downscale(rows, w, h, max_resolution)

    if _mark_mode:
        # Artwork on a clean field: the mark is the ONLY thing that may
        # displace the surface.  No pool, no frame, no mask shape — the
        # carve goes to zero at the artwork's own edges because the field
        # maps to zero, not because a boundary was drawn around it.
        rows = _flatten_field(rows, w, h)
    elif mask != "none":
        # A photo has content everywhere, so SOME boundary must exist; the
        # right one matches the PRODUCT (the proven coin look: a deliberate
        # inset pool), never the photo's own rectangle.  Enforced here in
        # heightmap space as the belt: whatever a style did or skipped
        # upstream — including a style that degraded because an optional
        # dependency was missing — the boundary below is the one that ships.
        rows = _mask_rows(rows, w, h, mask, style)

    if edge_enhance:
        rows = _sharpen(rows, w, h)

    if invert:
        rows = _invert(rows)

    # OpenSCAD surface() reads rows bottom-to-top; images are top-to-bottom.
    # flip_rows=True corrects this so the heightmap orientation matches the image.
    if flip_rows:
        rows = list(reversed(rows))

    os.makedirs(output_dir, exist_ok=True)
    stem = Path(image_path).stem
    dat_path = os.path.abspath(os.path.join(output_dir, f"{stem}_heightmap.dat"))
    _write_dat(dat_path, rows, w, h)

    return {
        "type": "heightmap",
        "dat_path": dat_path,
        "width_px": w,
        "height_px": h,
        "aspect_ratio": round(w / h, 4) if h > 0 else 1.0,
    }


def prepare_logo_image_for_emboss(
    image_path: str,
    output_dir: str,
    *,
    max_dim: int = 800,
) -> dict:
    """Prepare a bi-level raster logo as traced vector geometry.

    The heightmap path is wrong for marks: ``surface()`` spans the whole
    rectangular tile, so a logo picks up a background carve, a perimeter
    frame, a pixel staircase — and, without the row flip, mirrored text.
    Tracing the ink into native polygon() rings (see
    :func:`kiln.mark_geometry.trace_image_to_mark`) carves ONLY the
    strokes through the same proven boolean path SVG marks use.

    Returns a content_info dict of type ``"svg"`` (the boolean-carve
    contract).  Raises ``ValueError`` when nothing traceable is found so
    the caller can fall back to the heightmap path.
    """
    if _is_heic_container(image_path):
        os.makedirs(output_dir, exist_ok=True)
        image_path = _auto_convert_heic(image_path, output_dir)

    from kiln.mark_geometry import trace_image_to_mark

    mark = trace_image_to_mark(image_path, max_dim=max_dim)
    if mark is None or mark.is_empty:
        raise ValueError(f"No traceable mark found in image: {image_path}")
    return {
        "type": "svg",
        # Provenance only — the polygons below carry all the geometry.
        "svg_path": os.path.abspath(image_path),
        "width": mark.width,
        "height": mark.height,
        "aspect_ratio": round(mark.width / mark.height, 4) if mark.height else 1.0,
        "openscad_polygons": mark.to_scad(),
        # Real even-odd holes present — fill() would erase them.
        "openscad_polygons_fill_safe": False,
        **mark.content_bounds_info(),
        "traced_from_raster": True,
    }


def _convert_strokes_to_fills(svg_content: str, min_stroke_width: float = 0.0) -> str:
    """Convert SVG stroke-based elements to filled polygons for OpenSCAD.

    OpenSCAD's ``import()`` only understands filled geometry — strokes
    (lines, polylines, stroke-based paths) are silently ignored.  This
    converts ``<line>`` and ``<polyline>`` elements with visible strokes
    into thin filled ``<polygon>`` rectangles that OpenSCAD can extrude.

    Also converts ``<rect>`` elements with fill to ``<polygon>`` for
    maximum compatibility, and strips ``<text>`` elements (OpenSCAD
    can't render fonts from SVG).
    """
    # Convert <line> elements to filled rectangles
    def _line_to_polygon(match: re.Match[str]) -> str:
        attrs = match.group(0)
        x1_m = re.search(r'x1\s*=\s*"([^"]+)"', attrs)
        y1_m = re.search(r'y1\s*=\s*"([^"]+)"', attrs)
        x2_m = re.search(r'x2\s*=\s*"([^"]+)"', attrs)
        y2_m = re.search(r'y2\s*=\s*"([^"]+)"', attrs)
        sw_m = re.search(r'stroke-width\s*=\s*"([^"]+)"', attrs)
        color_m = re.search(r'stroke\s*=\s*"([^"]+)"', attrs)

        if not all([x1_m, y1_m, x2_m, y2_m]):
            return match.group(0)  # can't parse, leave as-is

        x1, y1 = float(x1_m.group(1)), float(y1_m.group(1))
        x2, y2 = float(x2_m.group(1)), float(y2_m.group(1))
        sw = float(sw_m.group(1)) if sw_m else 2.0
        if min_stroke_width > 0 and sw < min_stroke_width:
            sw = min_stroke_width
        color = color_m.group(1) if color_m else "#000000"

        # Compute perpendicular offset for stroke width
        import math as _math
        dx, dy = x2 - x1, y2 - y1
        length = _math.sqrt(dx * dx + dy * dy)
        if length < 0.001:
            return ""
        # Unit perpendicular
        px, py = -dy / length * sw / 2, dx / length * sw / 2

        # Four corners of the stroke rectangle
        pts = (
            f"{x1 + px:.2f},{y1 + py:.2f} "
            f"{x2 + px:.2f},{y2 + py:.2f} "
            f"{x2 - px:.2f},{y2 - py:.2f} "
            f"{x1 - px:.2f},{y1 - py:.2f}"
        )
        return f'<polygon points="{pts}" fill="{color}"/>'

    result = re.sub(r"<line\b[^>]*/>", _line_to_polygon, svg_content)

    # Strip <text> elements (OpenSCAD can't render them)
    result = re.sub(r"<text\b[^>]*>.*?</text>", "", result, flags=re.DOTALL)

    # Strip background <rect> that fills the entire viewBox (common in logos)
    result = re.sub(
        r'<rect\s+width\s*=\s*"512"\s+height\s*=\s*"512"[^/]*/>', "", result
    )

    # Strip small rects that were part of stripped <text> decoration.
    # These are orphaned elements (e.g. the orange accent mark between K and I
    # in "KILN") that survive text stripping because they're <rect> not <text>.
    # Detect by: small area (< 200 sq units) AND positioned in the lower
    # portion of the SVG (y > 60% of viewBox height) where text typically lives.
    def _strip_orphan_rects(match: re.Match[str]) -> str:
        attrs = match.group(0)
        ry_m = re.search(r'\by\s*=\s*"([^"]+)"', attrs)
        rw_m = re.search(r'\bwidth\s*=\s*"([^"]+)"', attrs)
        rh_m = re.search(r'\bheight\s*=\s*"([^"]+)"', attrs)
        if ry_m and rw_m and rh_m:
            try:
                ry = float(ry_m.group(1))
                rw = float(rw_m.group(1))
                rh = float(rh_m.group(1))
                # Small rect in text region — orphaned decoration
                if rw * rh < 200 and ry > 300:
                    return ""
            except ValueError:
                pass
        return match.group(0)

    result = re.sub(r"<rect\b[^>]*/?>", _strip_orphan_rects, result)

    return result


def _svg_to_openscad_polygons(svg_content: str) -> str:
    """Convert SVG geometry to native OpenSCAD polygon() calls.

    Bypasses OpenSCAD's unreliable SVG import() — native polygon()
    calls work reliably in difference() against any mesh complexity.
    This is the approach that made coaster v4's logo deboss work perfectly.

    **Y-axis flip:** SVG Y goes down (0=top), OpenSCAD Y goes up (0=bottom).
    All Y coordinates are mirrored: ``y_scad = svg_height - y_svg``.

    **Orphan filtering:** Small rects far from the main polygon cluster
    (e.g. text decoration marks that survived ``_convert_strokes_to_fills``)
    are stripped based on Y-distance from the polygon centroid.

    :returns: OpenSCAD code string with ``union() { polygon(...); ... }``
              or empty string if no extractable geometry.
    """
    import math as _math

    # Detect SVG height for Y-axis flip
    svg_height = 512.0  # default
    vb_match = re.search(r'viewBox\s*=\s*"([^"]+)"', svg_content, re.IGNORECASE)
    if vb_match:
        parts = vb_match.group(1).split()
        if len(parts) >= 4:
            with contextlib.suppress(ValueError):
                svg_height = float(parts[3])

    def _flip_y(y: float) -> float:
        return svg_height - y

    # Collect all polygon geometry with flipped Y.
    # 4-point polygons from stroke conversion → hull() pairs for clean corners.
    # This matches the coaster v4 proven approach: hull() { square at p1; square at p2; }
    # produces perfect tapered strokes without jagged corner overlap.
    poly_fragments: list[str] = []
    all_poly_y_values: list[float] = []

    for match in re.finditer(r'<polygon\b[^>]*points\s*=\s*"([^"]+)"', svg_content):
        points_str = match.group(1).strip()
        pts: list[tuple[float, float]] = []
        for pt in points_str.split():
            parts = pt.split(",")
            if len(parts) >= 2:
                try:
                    x, y = float(parts[0]), float(parts[1])
                    pts.append((x, _flip_y(y)))
                    all_poly_y_values.append(_flip_y(y))
                except ValueError:
                    continue

        if len(pts) == 4:
            # 4-point polygon — could be from stroke conversion or filled shape.
            # Detect if it's a thin stroke-like shape (aspect ratio > 2:1) and
            # use hull() pairs for clean corners, or emit raw polygon() if it's
            # a compact shape (like a square accent mark).
            import math as _m

            # Compute all 4 edge lengths to find the two short and two long edges
            edges = []
            for i in range(4):
                j = (i + 1) % 4
                d = _m.sqrt((pts[i][0]-pts[j][0])**2 + (pts[i][1]-pts[j][1])**2)
                edges.append((d, i, j))
            edges.sort(key=lambda e: e[0])

            short1, short2 = edges[0][0], edges[1][0]
            long1 = edges[2][0]

            if long1 > short1 * 2 and short1 > 0.5:
                # Stroke-like shape — use hull() for clean tapered corners.
                # Short edges connect the stroke endpoints; midpoints of
                # short edges are the original line endpoints.
                si, sj = edges[0][1], edges[0][2]
                p1x = (pts[si][0] + pts[sj][0]) / 2
                p1y = (pts[si][1] + pts[sj][1]) / 2

                si2, sj2 = edges[1][1], edges[1][2]
                p2x = (pts[si2][0] + pts[sj2][0]) / 2
                p2y = (pts[si2][1] + pts[sj2][1]) / 2

                sw = max(short1, short2, 1.0)

                poly_fragments.append(
                    f"hull() {{ translate([{p1x:.2f},{p1y:.2f}]) "
                    f"square([{sw:.2f},{sw:.2f}], center=true); "
                    f"translate([{p2x:.2f},{p2y:.2f}]) "
                    f"square([{sw:.2f},{sw:.2f}], center=true); }}"
                )
            else:
                # Compact shape — emit as raw polygon
                pts_scad = ", ".join(f"[{x:.2f},{y:.2f}]" for x, y in pts)
                poly_fragments.append(f"polygon(points=[{pts_scad}]);")
        elif len(pts) >= 3:
            # General polygon (not from stroke conversion)
            pts_scad = ", ".join(f"[{x:.2f},{y:.2f}]" for x, y in pts)
            poly_fragments.append(f"polygon(points=[{pts_scad}]);")

    # Compute centroid Y of all polygon geometry for orphan detection
    poly_center_y = (
        sum(all_poly_y_values) / len(all_poly_y_values)
        if all_poly_y_values else svg_height / 2
    )

    # Extract rects — skip background fills AND orphaned text-related rects
    rect_fragments: list[str] = []
    for match in re.finditer(r'<rect\b([^>]*)/?>', svg_content):
        attrs = match.group(1)
        rw_m = re.search(r'\bwidth\s*=\s*"([^"]+)"', attrs)
        rh_m = re.search(r'\bheight\s*=\s*"([^"]+)"', attrs)
        if not (rw_m and rh_m):
            continue
        try:
            rw = float(rw_m.group(1))
            rh = float(rh_m.group(1))
        except ValueError:
            continue
        # Skip viewBox-filling background rects (covers >90% of viewBox)
        if rw >= svg_height * 0.9 and rh >= svg_height * 0.9:
            continue
        rx_m = re.search(r'\bx\s*=\s*"([^"]+)"', attrs)
        ry_m = re.search(r'\by\s*=\s*"([^"]+)"', attrs)
        rx = float(rx_m.group(1)) if rx_m else 0.0
        ry = float(ry_m.group(1)) if ry_m else 0.0
        ry_flipped = _flip_y(ry + rh)  # bottom-left in OpenSCAD coords

        # Filter orphaned rects: if the rect center is >100 SVG units
        # from the polygon centroid Y, it's likely a text decoration remnant
        rect_center_y = ry_flipped + rh / 2
        if all_poly_y_values and abs(rect_center_y - poly_center_y) > 100:
            continue  # orphaned text-related rect

        rect_fragments.append(
            f"translate([{rx:.2f},{ry_flipped:.2f}]) square([{rw:.2f},{rh:.2f}]);"
        )

    # Extract <circle cx="..." cy="..." r="..."> elements
    circle_fragments: list[str] = []
    for match in re.finditer(
        r'<circle\b[^>]*?cx\s*=\s*"([^"]+)"[^>]*?cy\s*=\s*"([^"]+)"'
        r'[^>]*?r\s*=\s*"([^"]+)"',
        svg_content,
    ):
        try:
            cx = float(match.group(1))
            cy = float(match.group(2))
            r = float(match.group(3))
        except ValueError:
            continue
        cy_flipped = _flip_y(cy)
        n = 60
        pts = []
        for i in range(n):
            a = 2 * _math.pi * i / n
            pts.append((cx + r * _math.cos(a), cy_flipped + r * _math.sin(a)))
        pts_scad = ", ".join(f"[{x:.2f},{y:.2f}]" for x, y in pts)
        circle_fragments.append(f"polygon(points=[{pts_scad}]);")

    # Combine all fragments
    all_fragments = poly_fragments + rect_fragments + circle_fragments
    if not all_fragments:
        return ""

    body = "\n    ".join(all_fragments)
    return f"union() {{\n    {body}\n}}"


def prepare_svg_for_emboss(svg_path: str, output_dir: str, *, min_physical_width_mm: float = 0.8, target_size_mm: float = 0.0) -> dict:
    """Validate and prepare an SVG file for use with OpenSCAD's import().

    Automatically converts stroke-based SVGs (lines, polylines) to
    filled polygons, since OpenSCAD only understands filled geometry.

    Parameters
    ----------
    svg_path : str
        Path to the SVG file.
    output_dir : str
        Output directory for the processed SVG.

    Returns
    -------
    dict
        Keys: type, svg_path (absolute), width, height, aspect_ratio
    """
    abs_path = os.path.abspath(svg_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"SVG file not found: {abs_path}")

    with open(abs_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    if "<svg" not in content.lower():
        raise ValueError(f"File does not appear to be a valid SVG: {abs_path}")

    # Extract viewBox dimensions
    width: float | None = None
    height: float | None = None

    vb_match = re.search(r'viewBox\s*=\s*"([^"]+)"', content, re.IGNORECASE)
    if vb_match:
        parts = vb_match.group(1).split()
        if len(parts) >= 4:
            try:
                width = float(parts[2])
                height = float(parts[3])
            except ValueError:
                pass

    # Fallback: explicit width/height attributes
    if width is None:
        w_match = re.search(r'<svg[^>]+width\s*=\s*"([\d.]+)', content, re.IGNORECASE)
        if w_match:
            width = float(w_match.group(1))
    if height is None:
        h_match = re.search(
            r'<svg[^>]+height\s*=\s*"([\d.]+)', content, re.IGNORECASE
        )
        if h_match:
            height = float(h_match.group(1))

    # Defaults if we cannot determine dimensions
    if width is None:
        width = 100.0
    if height is None:
        height = 100.0

    aspect = round(width / height, 4) if height > 0 else 1.0

    # Calculate minimum stroke width for printability.
    # Target: each stroke should be at least 2 nozzle widths (0.8mm)
    # when printed, for reliable visibility.
    # min_physical_mm = 0.8 (2x nozzle for clean lines)
    # scale_factor = face_size / svg_size (estimated)
    # min_svg_units = min_physical_mm / scale_factor
    # Since we don't know the face size here, use a conservative estimate:
    # typical face coverage is 0.7 * 90mm = 63mm for a coaster
    # For a 512-unit SVG: 0.8mm / (63/512) = 6.5 SVG units
    # For safety, use: min_stroke = max(original * 3, svg_dimension / 80)
    # This ensures strokes are at least 1.25% of the SVG dimension,
    # which maps to ~0.8mm at typical print scales.
    if target_size_mm > 0:
        svg_size = max(width, height)
        scale = target_size_mm / svg_size if svg_size > 0 else 1.0
        min_sw = min_physical_width_mm / scale
    else:
        min_sw = max(width, height) / 80.0  # conservative fallback

    # Primary path: compile the SVG with the real parser (full path
    # command set, shapes, transforms, even-odd holes) into
    # origin-centered native polygon() geometry — see kiln.mark_geometry.
    # This replaces regex extraction that only understood
    # <polygon>/<rect>/<circle> and dropped every <path>-based logo,
    # and it centers by construction so offset-origin viewBoxes place
    # correctly.  The legacy machinery below runs only when the parser
    # finds no geometry (e.g. text-only SVGs).
    try:
        from kiln.mark_geometry import parse_svg_to_mark

        mark = parse_svg_to_mark(content, min_stroke_units=min_sw)
    except Exception:  # noqa: BLE001 — parser bugs must fall back, not fail
        _logger.warning("SVG mark parse crashed — using legacy path", exc_info=True)
        mark = None
    if mark is not None and not mark.is_empty:
        return {
            "type": "svg",
            "svg_path": abs_path,
            "width": width,
            "height": height,
            "aspect_ratio": (
                round(mark.width / mark.height, 4) if mark.height else aspect
            ),
            "openscad_polygons": mark.to_scad(),
            # Real even-odd holes present — fill() would erase them.
            "openscad_polygons_fill_safe": False,
            **mark.content_bounds_info(),
        }

    # Convert strokes to fills for OpenSCAD compatibility
    has_strokes = "<line" in content or "stroke" in content
    if has_strokes:
        processed = _convert_strokes_to_fills(content, min_stroke_width=min_sw)
        os.makedirs(output_dir, exist_ok=True)
        stem = Path(svg_path).stem
        processed_path = os.path.abspath(
            os.path.join(output_dir, f"{stem}_emboss.svg")
        )
        with open(processed_path, "w", encoding="utf-8") as f:
            f.write(processed)
        abs_path = processed_path

    # Calculate bounding box of actual geometry (polygons, rects, circles, paths)
    # so the emboss generator can scale based on content, not viewBox
    with open(abs_path, encoding="utf-8", errors="replace") as f:
        final_svg = f.read()

    content_x_min: float | None = None
    content_y_min: float | None = None
    content_x_max: float | None = None
    content_y_max: float | None = None

    def _update_bounds(x: float, y: float) -> None:
        nonlocal content_x_min, content_y_min, content_x_max, content_y_max
        if content_x_min is None or x < content_x_min:
            content_x_min = x
        if content_x_max is None or x > content_x_max:
            content_x_max = x
        if content_y_min is None or y < content_y_min:
            content_y_min = y
        if content_y_max is None or y > content_y_max:
            content_y_max = y

    # Parse <polygon points="x1,y1 x2,y2 ..."> elements
    for poly_match in re.finditer(r'<polygon\b[^>]*points\s*=\s*"([^"]+)"', final_svg):
        points_str = poly_match.group(1).strip()
        for pt in points_str.split():
            parts = pt.split(",")
            if len(parts) >= 2:
                with contextlib.suppress(ValueError):
                    _update_bounds(float(parts[0]), float(parts[1]))

    # Parse <circle cx="..." cy="..." r="..."> elements
    for circ_match in re.finditer(
        r'<circle\b[^>]*?cx\s*=\s*"([^"]+)"[^>]*?cy\s*=\s*"([^"]+)"[^>]*?r\s*=\s*"([^"]+)"',
        final_svg,
    ):
        try:
            cx_c, cy_c, r_c = float(circ_match.group(1)), float(circ_match.group(2)), float(circ_match.group(3))
            _update_bounds(cx_c - r_c, cy_c - r_c)
            _update_bounds(cx_c + r_c, cy_c + r_c)
        except ValueError:
            pass

    # Parse <rect x="..." y="..." width="..." height="..."> elements
    for rect_match in re.finditer(r'<rect\b([^>]*)/?>', final_svg):
        rattrs = rect_match.group(1)
        rx_m = re.search(r'\bx\s*=\s*"([^"]+)"', rattrs)
        ry_m = re.search(r'\by\s*=\s*"([^"]+)"', rattrs)
        rw_m = re.search(r'\bwidth\s*=\s*"([^"]+)"', rattrs)
        rh_m = re.search(r'\bheight\s*=\s*"([^"]+)"', rattrs)
        if rw_m and rh_m:
            try:
                rx = float(rx_m.group(1)) if rx_m else 0.0
                ry = float(ry_m.group(1)) if ry_m else 0.0
                rw = float(rw_m.group(1))
                rh = float(rh_m.group(1))
                _update_bounds(rx, ry)
                _update_bounds(rx + rw, ry + rh)
            except ValueError:
                pass

    # Extract native OpenSCAD polygon geometry from SVG.
    # This bypasses OpenSCAD's unreliable SVG import() — native polygon()
    # calls work reliably in difference() against any mesh complexity.
    # Proven approach: coaster v4 used native square()/hull() geometry.
    openscad_polygons = _svg_to_openscad_polygons(final_svg)

    result = {
        "type": "svg",
        "svg_path": abs_path,
        "width": width,
        "height": height,
        "aspect_ratio": aspect,
    }

    if openscad_polygons:
        result["openscad_polygons"] = openscad_polygons

    # Add content bounds if we found any geometry
    if content_x_min is not None and content_x_max is not None:
        content_width = content_x_max - content_x_min
        content_height = (content_y_max or 0) - (content_y_min or 0)
        if content_width > 0 and content_height > 0:
            result["content_x_min"] = round(content_x_min, 4)
            result["content_y_min"] = round(content_y_min or 0, 4)
            result["content_width"] = round(content_width, 4)
            result["content_height"] = round(content_height, 4)

    return result


def generate_text_image(
    text: str, output_dir: str, *, font_size: int | None = None
) -> dict:
    """Return metadata for OpenSCAD native text() module.

    Rather than rendering text to a raster image (which would require
    font rendering libraries), this returns the OpenSCAD fragment that
    can be used directly in .scad files.

    Parameters
    ----------
    text : str
        The text string to emboss.
    output_dir : str
        Output directory (unused, kept for API consistency).
    font_size : int | None
        Explicit font size for OpenSCAD text().  Default ``None`` lets
        the emboss generator MEASURE the rendered text and size it to
        fit the target face (the safe path).  The old default of 48 was
        a silent overflow machine: the generator honours an explicit
        size verbatim, and "KILN" at 48 renders 146mm wide — off both
        edges of a 90mm coaster.  Pass a size only when the caller owns
        the layout (multi-line typography); even then the generator
        clamps it down if its measured bbox would overflow the face.

    Returns
    -------
    dict
        Keys: type, text, openscad_fragment — plus font_size when given.
    """
    # Escape quotes for OpenSCAD string literal
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    nominal = font_size if font_size else 48
    fragment = (
        f'text("{escaped}", size={nominal}, '
        f'halign="center", valign="center", '
        f'font="Liberation Sans:style=Bold");'
    )
    info: dict = {
        "type": "openscad_text",
        "text": text,
        "openscad_fragment": fragment,
    }
    if font_size:
        info["font_size"] = font_size
    return info


def rasterize_svg_to_png(
    svg_path: str,
    output_png: str,
    *,
    width_px: int = 1024,
) -> str:
    """Rasterize an SVG file to a high-resolution PNG.

    This is the fallback path for the emboss pipeline when OpenSCAD's
    native ``import()`` of SVG produces degenerate geometry (common with
    thin stroke-to-fill conversions).  The resulting PNG can be fed into
    :func:`prepare_image_for_emboss` to produce a DAT heightmap that
    OpenSCAD's ``surface()`` handles reliably.

    Tries, in order:
    1. ``rsvg-convert`` (librsvg — fast, accurate)
    2. ``sips`` (macOS built-in — always available on Mac)
    3. ``convert`` (ImageMagick)
    4. ``inkscape --export-png`` (Inkscape CLI)

    Raises :class:`RuntimeError` if no rasterizer is available.

    Parameters
    ----------
    svg_path : str
        Input SVG file path.
    output_png : str
        Output PNG file path.
    width_px : int
        Target width in pixels (height auto-scales to preserve aspect).

    Returns
    -------
    str
        Absolute path to the output PNG (same as *output_png*).
    """
    import shutil

    abs_svg = os.path.abspath(svg_path)
    abs_png = os.path.abspath(output_png)

    if not os.path.isfile(abs_svg):
        raise FileNotFoundError(f"SVG file not found: {abs_svg}")

    os.makedirs(os.path.dirname(abs_png) or ".", exist_ok=True)

    # --- Try rsvg-convert ---
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        result = subprocess.run(
            [rsvg, "-w", str(width_px), "-o", abs_png, abs_svg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.isfile(abs_png):
            return abs_png

    # --- Try sips (macOS) ---
    sips = shutil.which("sips")
    if sips:
        # sips can convert SVG to PNG on macOS
        result = subprocess.run(
            [sips, "-s", "format", "png", "-z", str(width_px), str(width_px),
             abs_svg, "--out", abs_png],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.isfile(abs_png):
            return abs_png

    # --- Try ImageMagick convert ---
    convert = shutil.which("magick") or shutil.which("convert")
    if convert:
        result = subprocess.run(
            [convert, "-background", "white", "-flatten",
             "-resize", f"{width_px}x", abs_svg, abs_png],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.isfile(abs_png):
            return abs_png

    # --- Try Inkscape ---
    inkscape = shutil.which("inkscape")
    if inkscape:
        result = subprocess.run(
            [inkscape, "--export-type=png", f"--export-filename={abs_png}",
             f"--export-width={width_px}", abs_svg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.isfile(abs_png):
            return abs_png

    raise RuntimeError(
        "No SVG rasterizer found. Install one of: librsvg (rsvg-convert), "
        "ImageMagick (magick/convert), or Inkscape. On macOS, sips should "
        "be available but failed for this SVG."
    )


def generate_qr_data(content: str, output_dir: str) -> dict:
    """Generate a QR code heightmap — requires kiln-pro (Pro feature).

    QR code decoration is a paid feature. The implementation lives in
    the kiln-pro package. This stub raises ImportError to direct users
    to upgrade.

    See ``kiln_pro.decoration.qr_decorator.generate_qr_data`` for the
    real implementation.
    """
    raise ImportError(
        "QR code decoration is a Pro feature. "
        "Upgrade at https://kiln3d.com/pricing"
    )
