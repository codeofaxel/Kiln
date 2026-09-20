"""A QOI image encoder, in plain Python, for the thumbnails printer screens read.

QOI ("Quite OK Image", qoiformat.org, specification version 1.0 of
2022-01-05) is the format the Prusa MK4 / MINI / XL / Core One screens
(Prusa-Firmware-Buddy 5.1.0 and later) and Duet's PanelDue (firmware 3.5
and later) decode out of a G-code file's thumbnail comment blocks.  Neither
Pillow nor the standard library can WRITE it (Pillow reads it, from 9.5),
so the encoder lives here.  It mirrors the reference ``qoi_encode`` in
phoboslab/qoi ``qoi.h`` op for op — the same choice of chunk at every
pixel — so its output is byte for byte what the reference encoder writes,
which is what :mod:`kiln.tests` pins it against.

THE FORMAT, from the specification (one page):

* A 14-byte header: ``"qoif"``, width and height as big-endian uint32,
  ``channels`` (3 = RGB, 4 = RGBA), ``colorspace`` (0 = sRGB with linear
  alpha, 1 = all linear).  The last two are informative only; they do not
  change how the chunks are coded.
* Pixels row by row, left to right, top to bottom.  Encoder and decoder
  both start from a previous pixel of ``{0, 0, 0, 255}`` and a
  zero-initialised 64-entry index of previously seen pixels, addressed by
  ``(r * 3 + g * 5 + b * 7 + a * 11) % 64``.
* Six chunk kinds, the 8-bit tags checked before the 2-bit ones:
  ``QOI_OP_INDEX`` (``00iiiiii``), ``QOI_OP_DIFF`` (``01rrggbb``, each a
  -2..1 difference stored with bias 2), ``QOI_OP_LUMA`` (``10gggggg`` +
  ``rrrrbbbb``: green difference -32..31 with bias 32, red and blue
  differences relative to green -8..7 with bias 8), ``QOI_OP_RUN``
  (``11rrrrrr``, a run of 1..62 of the previous pixel with bias -1; 63 and
  64 are taken by the two 8-bit tags), ``QOI_OP_RGB`` (``0xFE`` + r g b) and
  ``QOI_OP_RGBA`` (``0xFF`` + r g b a).  Differences wrap modulo 256.
* An 8-byte end marker: seven ``0x00`` bytes and one ``0x01``.

Only the encoder ships.  The decoder that proves it lives with the tests,
where the reference test images from qoiformat.org are round-tripped.
"""

from __future__ import annotations

import struct

#: The header's magic bytes.
QOI_MAGIC = b"qoif"

#: Header length in bytes: magic, width, height, channels, colorspace.
QOI_HEADER_SIZE = 14

#: The stream ends with seven zero bytes and a one.
QOI_END_MARKER = b"\x00" * 7 + b"\x01"

#: ``qoi.h`` refuses images of 400 million pixels or more; so does this.
QOI_PIXELS_MAX = 400_000_000

#: ``colorspace`` values, informative only.
QOI_SRGB = 0
QOI_LINEAR = 1

_OP_INDEX = 0x00
_OP_DIFF = 0x40
_OP_LUMA = 0x80
_OP_RUN = 0xC0
_OP_RGB = 0xFE
_OP_RGBA = 0xFF


def _signed8(value: int) -> int:
    """*value* as a C ``signed char``: the wraparound the spec's differences use."""
    return ((value + 128) & 0xFF) - 128


def qoi_encode(
    pixels: bytes | bytearray | memoryview,
    width: int,
    height: int,
    channels: int = 4,
    colorspace: int = QOI_SRGB,
) -> bytes:
    """Encode *pixels* (row-major, top row first, *channels* bytes each) as QOI.

    Exactly the reference encoder's choices: a run is closed at 62 or at the
    last pixel; an index hit is preferred to a difference; a difference to
    a luma chunk; a luma chunk to a full RGB; and any change in alpha is a
    full RGBA.  The index is written only when a pixel is coded explicitly
    — never for a run or an index hit — which is what keeps a reference
    decoder's index in step with this encoder's.

    Raises :class:`ValueError` for a header the reference decoder would
    refuse or a pixel buffer that is not ``width * height * channels`` long.
    """
    if width <= 0 or height <= 0:
        raise ValueError("QOI needs a positive width and height")
    if channels not in (3, 4):
        raise ValueError("QOI carries 3 (RGB) or 4 (RGBA) channels")
    if colorspace not in (QOI_SRGB, QOI_LINEAR):
        raise ValueError("QOI colorspace is 0 (sRGB) or 1 (linear)")
    if height >= QOI_PIXELS_MAX // width:
        raise ValueError("image too large for QOI")
    data = bytes(pixels)
    if len(data) != width * height * channels:
        raise ValueError(
            f"expected {width * height * channels} bytes of pixels for "
            f"{width}x{height}x{channels}, got {len(data)}"
        )

    out = bytearray(struct.pack(">4sIIBB", QOI_MAGIC, width, height, channels, colorspace))
    index = [(0, 0, 0, 0)] * 64
    prev = (0, 0, 0, 255)
    run = 0
    last = width * height - 1

    for n in range(width * height):
        at = n * channels
        if channels == 4:
            px = (data[at], data[at + 1], data[at + 2], data[at + 3])
        else:
            px = (data[at], data[at + 1], data[at + 2], prev[3])

        if px == prev:
            run += 1
            if run == 62 or n == last:
                out.append(_OP_RUN | (run - 1))
                run = 0
            continue

        if run > 0:
            out.append(_OP_RUN | (run - 1))
            run = 0

        r, g, b, a = px
        slot = (r * 3 + g * 5 + b * 7 + a * 11) & 63
        if index[slot] == px:
            out.append(_OP_INDEX | slot)
        else:
            index[slot] = px
            if a == prev[3]:
                vr = _signed8(r - prev[0])
                vg = _signed8(g - prev[1])
                vb = _signed8(b - prev[2])
                vg_r = _signed8(vr - vg)
                vg_b = _signed8(vb - vg)
                if -3 < vr < 2 and -3 < vg < 2 and -3 < vb < 2:
                    out.append(_OP_DIFF | (vr + 2) << 4 | (vg + 2) << 2 | (vb + 2))
                elif -9 < vg_r < 8 and -33 < vg < 32 and -9 < vg_b < 8:
                    out.append(_OP_LUMA | (vg + 32))
                    out.append((vg_r + 8) << 4 | (vg_b + 8))
                else:
                    out.extend((_OP_RGB, r, g, b))
            else:
                out.extend((_OP_RGBA, r, g, b, a))
        prev = px

    out.extend(QOI_END_MARKER)
    return bytes(out)


def qoi_dimensions(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a QOI header, or ``None`` when *data* is not one.

    The same header check the readers apply before decoding — PanelDue's
    ``qoi_decode_header`` refuses a wrong magic, a zero dimension, a channel
    count outside 3..4 or a colorspace above 1; the Buddy screen reads the
    dimensions from bytes 4..11 and nothing else.
    """
    if len(data) < QOI_HEADER_SIZE or data[:4] != QOI_MAGIC:
        return None
    width, height, channels, colorspace = struct.unpack(">IIBB", data[4:QOI_HEADER_SIZE])
    if width == 0 or height == 0 or channels not in (3, 4) or colorspace > 1:
        return None
    return width, height


def qoi_encode_png(png: bytes) -> bytes | None:
    """The QOI encoding of a PNG, RGBA, sRGB — or ``None`` without Pillow.

    RGBA whatever the PNG carried, so a transparent background stays
    transparent: the Prusa screen mixes alpha against black, PanelDue drops
    it and draws the colour underneath, which for a transparent pixel from
    Kiln's own renderer is black.  Both are the screens' own backgrounds.
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(png)) as img:
        rgba = img.convert("RGBA")
        return qoi_encode(rgba.tobytes(), rgba.width, rgba.height, 4, QOI_SRGB)
