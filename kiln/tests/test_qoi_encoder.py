"""The QOI encoder writes what the reference encoder writes, byte for byte.

QOI is the picture format the Prusa MK4 / MINI / XL / Core One screens and
Duet's PanelDue decode out of a G-code thumbnail block.  Kiln's encoder
(:mod:`kiln.printers.qoi`) is pinned three ways:

1. **Reference vectors.**  The expected bytes below were produced by the
   reference implementation — phoboslab/qoi ``qoi.h`` at commit 97bacc8
   (2026-05-29), compiled and run on 2026-09-19 — for small images that
   between them exercise every chunk kind the specification defines
   (``QOI_OP_RUN``, ``INDEX``, ``DIFF``, ``LUMA``, ``RGB``, ``RGBA``), the
   run boundary at 62, the wraparound difference (255 to 0 is +1), and both
   channel counts.  The same session encoded the eight official test
   images from qoiformat.org (dice, kodim10, kodim23, qoi_logo, testcard,
   testcard_rgba, wikipedia_008, edgecase) and got the reference's bytes
   for all eight; those files are 5.6 MB and are not committed.
2. **A decoder written here from the specification**, independent of the
   encoder, round-trips every vector and a set of seeded random images.
3. **Pillow's own QOI reader** (an implementation Kiln did not write)
   agrees with the decoder here, when Pillow is new enough to have one.
"""

from __future__ import annotations

import io
import random
import struct

import pytest

from kiln.printers import qoi

# ---------------------------------------------------------------------------
# Reference vectors: (pixels, width, height, channels) -> bytes from qoi.h
# ---------------------------------------------------------------------------

_A = (10, 20, 30)
_B = (200, 100, 50)


def _flat(pixels) -> bytes:
    return bytes(v for px in pixels for v in px)


#: name -> (pixels, width, height, channels, reference hex)
REFERENCE_VECTORS: dict[str, tuple[bytes, int, int, int, str]] = {
    # Equals the starting pixel {0,0,0,255}: one run.
    "black_1x1_rgba": (
        bytes([0, 0, 0, 255]), 1, 1, 4,
        "716f696600000001000000010400" "c0" "0000000000000001",
    ),
    # 255 from 0 wraps to a difference of -1: QOI_OP_DIFF, not RGB.
    "red_1x1_rgba": (
        bytes([255, 0, 0, 255]), 1, 1, 4,
        "716f696600000001000000010400" "5a" "0000000000000001",
    ),
    # Too far for DIFF or LUMA: QOI_OP_RGB, and a 3-channel header.
    "grey_1x1_rgb": (
        bytes([128, 128, 128]), 1, 1, 3,
        "716f696600000001000000010300" "fe808080" "0000000000000001",
    ),
    # Every chunk kind in eight pixels: RUN(2) RGB DIFF LUMA INDEX RGBA RUN(1).
    "every_op_4x2_rgba": (
        _flat([
            (0, 0, 0, 255), (0, 0, 0, 255),
            (10, 20, 30, 255),   # RGB
            (11, 21, 31, 255),   # DIFF +1 +1 +1
            (21, 29, 35, 255),   # LUMA: dg=8, dr-dg=2, db-dg=-4
            (10, 20, 30, 255),   # INDEX hit
            (10, 20, 30, 128),   # RGBA: alpha changed
            (10, 20, 30, 128),   # a run of one, closed at the last pixel
        ]), 4, 2, 4,
        "716f696600000004000000020400" "c1" "fe0a141e" "7f" "a8a4" "09" "ff0a141e80" "c0"
        "0000000000000001",
    ),
    # A run closes at 62; the remaining 8 close at the last pixel.
    "run_70x1_rgb": (
        bytes([7, 8, 9]) * 70, 70, 1, 3,
        "716f696600000046000000010300" "a879fd" "c6" "0000000000000001",
    ),
    # Two colours alternating: RGB RGB then index hits at slots 9 and 31.
    "index_abab_6x1_rgb": (
        _flat([_A, _B, _A, _B, _A, _B]), 6, 1, 3,
        "716f696600000006000000010300" "fe0a141e" "fec86432" "091f091f" "0000000000000001",
    ),
    # -1 wraps to 255 and +1 wraps back to 0, both as DIFF; then an index hit.
    "wrap_3x1_rgba": (
        bytes([255, 255, 255, 255, 0, 0, 0, 255, 255, 255, 255, 255]), 3, 1, 4,
        "716f696600000003000000010400" "55" "7f" "26" "0000000000000001",
    ),
}


# ---------------------------------------------------------------------------
# A decoder, from the specification, for the tests only
# ---------------------------------------------------------------------------


def qoi_decode(data: bytes) -> tuple[int, int, int, bytes]:
    """``(width, height, channels, rgba_pixels)`` — the spec, chunk by chunk.

    Written from the specification text rather than from the encoder, so
    an error the two might share has to be in the reading of the spec.
    Raises :class:`ValueError` on a header or stream the spec forbids.
    """
    if len(data) < 22 or data[:4] != b"qoif":
        raise ValueError("not a QOI stream")
    width, height, channels, colorspace = struct.unpack(">IIBB", data[4:14])
    if width == 0 or height == 0 or channels not in (3, 4) or colorspace > 1:
        raise ValueError("bad QOI header")
    if data[-8:] != b"\x00" * 7 + b"\x01":
        raise ValueError("missing end marker")
    body = data[14:-8]
    out = bytearray()
    index = [(0, 0, 0, 0)] * 64
    px = (0, 0, 0, 255)
    total = width * height
    p = 0
    while len(out) < total * 4:
        if p >= len(body):
            raise ValueError("stream ended before the last pixel")
        b1 = body[p]
        p += 1
        if b1 == 0xFE:
            px = (body[p], body[p + 1], body[p + 2], px[3])
            p += 3
        elif b1 == 0xFF:
            px = (body[p], body[p + 1], body[p + 2], body[p + 3])
            p += 4
        elif b1 >> 6 == 0b00:
            px = index[b1 & 0x3F]
        elif b1 >> 6 == 0b01:
            px = (
                (px[0] + ((b1 >> 4) & 3) - 2) & 0xFF,
                (px[1] + ((b1 >> 2) & 3) - 2) & 0xFF,
                (px[2] + (b1 & 3) - 2) & 0xFF,
                px[3],
            )
        elif b1 >> 6 == 0b10:
            b2 = body[p]
            p += 1
            dg = (b1 & 0x3F) - 32
            px = (
                (px[0] + dg - 8 + ((b2 >> 4) & 0x0F)) & 0xFF,
                (px[1] + dg) & 0xFF,
                (px[2] + dg - 8 + (b2 & 0x0F)) & 0xFF,
                px[3],
            )
        else:
            run = (b1 & 0x3F) + 1
            if run > 62:
                raise ValueError("run lengths 63 and 64 are not legal")
            out.extend(bytes(px) * min(run, total - len(out) // 4))
            index[(px[0] * 3 + px[1] * 5 + px[2] * 7 + px[3] * 11) % 64] = px
            continue
        index[(px[0] * 3 + px[1] * 5 + px[2] * 7 + px[3] * 11) % 64] = px
        out.extend(px)
    if p != len(body):
        raise ValueError("trailing bytes after the last pixel")
    return width, height, channels, bytes(out)


def _as_rgba(pixels: bytes, channels: int) -> bytes:
    if channels == 4:
        return pixels
    return b"".join(pixels[i:i + 3] + b"\xff" for i in range(0, len(pixels), 3))


# ---------------------------------------------------------------------------
# The vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(REFERENCE_VECTORS))
def test_the_reference_encoders_bytes_exactly(name):
    pixels, width, height, channels, expected = REFERENCE_VECTORS[name]
    assert qoi.qoi_encode(pixels, width, height, channels).hex() == expected


@pytest.mark.parametrize("name", sorted(REFERENCE_VECTORS))
def test_the_reference_bytes_decode_to_the_pixels(name):
    pixels, width, height, channels, expected = REFERENCE_VECTORS[name]
    w, h, c, rgba = qoi_decode(bytes.fromhex(expected))
    assert (w, h, c) == (width, height, channels)
    assert rgba == _as_rgba(pixels, channels)


def test_every_chunk_kind_appears_in_the_vectors():
    """The vectors are only a pin if between them they reach every branch."""
    seen: set[str] = set()
    for _, _, _, _, expected in REFERENCE_VECTORS.values():
        body = bytes.fromhex(expected)[14:-8]
        p = 0
        while p < len(body):
            b1 = body[p]
            if b1 == 0xFE:
                seen.add("RGB")
                p += 4
            elif b1 == 0xFF:
                seen.add("RGBA")
                p += 5
            else:
                seen.add(("INDEX", "DIFF", "LUMA", "RUN")[b1 >> 6])
                p += 2 if b1 >> 6 == 0b10 else 1
    assert seen == {"RGB", "RGBA", "INDEX", "DIFF", "LUMA", "RUN"}


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def _random_image(seed: int, width: int, height: int, channels: int, flat: bool) -> bytes:
    """Noise, or the flat-shaded kind of picture a print preview is."""
    rng = random.Random(seed)
    if flat:
        palette = [tuple(rng.randrange(256) for _ in range(channels)) for _ in range(6)]
        rows = []
        for _y in range(height):
            row = []
            colour = rng.choice(palette)
            for _x in range(width):
                if rng.random() < 0.05:
                    colour = rng.choice(palette)
                row.extend(colour)
            rows.append(bytes(row))
        return b"".join(rows)
    return bytes(rng.randrange(256) for _ in range(width * height * channels))


@pytest.mark.parametrize("channels", [3, 4])
@pytest.mark.parametrize("flat", [True, False])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_images_round_trip(seed, flat, channels):
    width, height = 37, 23
    pixels = _random_image(seed, width, height, channels, flat)
    encoded = qoi.qoi_encode(pixels, width, height, channels)
    w, h, c, rgba = qoi_decode(encoded)
    assert (w, h, c) == (width, height, channels)
    assert rgba == _as_rgba(pixels, channels)


def test_a_valid_encoder_never_repeats_an_index_chunk():
    """The spec: 'A valid encoder must not issue 2 or more consecutive
    QOI_OP_INDEX chunks to the same index.'  A repeat is a run."""
    pixels = _random_image(7, 64, 16, 4, flat=True)
    body = qoi.qoi_encode(pixels, 64, 16, 4)[14:-8]
    p = 0
    last_index = None
    while p < len(body):
        b1 = body[p]
        if b1 == 0xFE:
            p += 4
            last_index = None
        elif b1 == 0xFF:
            p += 5
            last_index = None
        elif b1 >> 6 == 0b00:
            assert b1 != last_index, "two consecutive INDEX chunks to the same slot"
            last_index = b1
            p += 1
        elif b1 >> 6 == 0b10:
            p += 2
            last_index = None
        else:
            p += 1
            last_index = None


def test_runs_are_never_longer_than_62():
    """1000 of the starting pixel: sixteen full runs and one of eight."""
    body = qoi.qoi_encode(bytes([0, 0, 0, 255]) * 1000, 1000, 1, 4)[14:-8]
    assert body == bytes([0xC0 | 61] * 16 + [0xC0 | (1000 - 62 * 16 - 1)])


# ---------------------------------------------------------------------------
# Header rules, and the two helpers gcode_complete uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "width,height,channels,colorspace",
    [(0, 1, 4, 0), (1, 0, 4, 0), (1, 1, 2, 0), (1, 1, 5, 0), (1, 1, 4, 2)],
)
def test_a_header_the_reference_decoder_refuses_is_not_written(width, height, channels, colorspace):
    with pytest.raises(ValueError):
        qoi.qoi_encode(b"\x00" * 8, width, height, channels, colorspace)


def test_a_short_pixel_buffer_is_refused():
    with pytest.raises(ValueError):
        qoi.qoi_encode(b"\x00" * 7, 2, 1, 4)


def test_qoi_dimensions_reads_the_header_and_nothing_else():
    encoded = qoi.qoi_encode(bytes([1, 2, 3, 4]) * 6, 3, 2, 4)
    assert qoi.qoi_dimensions(encoded) == (3, 2)
    assert qoi.qoi_dimensions(encoded[:13]) is None
    assert qoi.qoi_dimensions(b"\x89PNG\r\n\x1a\n" + encoded[8:]) is None
    assert qoi.qoi_dimensions(encoded[:12] + b"\x02\x00" + encoded[14:]) is None


def test_a_png_becomes_an_rgba_qoi_of_the_same_picture():
    from PIL import Image

    img = Image.new("RGB", (8, 5), (200, 60, 60))
    img.putpixel((3, 2), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = qoi.qoi_encode_png(buf.getvalue())
    assert encoded is not None
    w, h, c, rgba = qoi_decode(encoded)
    assert (w, h, c) == (8, 5, 4)
    assert rgba == img.convert("RGBA").tobytes()


def test_pillows_own_reader_agrees():
    """An implementation Kiln did not write decodes what Kiln wrote."""
    from PIL import Image

    try:
        from PIL import QoiImagePlugin  # noqa: F401 — Pillow >= 9.5
    except ImportError:
        pytest.skip("this Pillow has no QOI reader")
    for name, (pixels, width, height, channels, _) in REFERENCE_VECTORS.items():
        encoded = qoi.qoi_encode(pixels, width, height, channels)
        with Image.open(io.BytesIO(encoded)) as img:
            assert img.size == (width, height), name
            assert img.convert("RGBA").tobytes() == _as_rgba(pixels, channels), name
    pixels = _random_image(11, 50, 40, 4, flat=False)
    with Image.open(io.BytesIO(qoi.qoi_encode(pixels, 50, 40, 4))) as img:
        assert img.convert("RGBA").tobytes() == pixels
