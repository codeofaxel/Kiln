"""Tests for kiln.image_to_surface module."""

from __future__ import annotations

import os
import struct
import zlib

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_svg(extra_content: str = "") -> str:
    """Return a minimal valid SVG string."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        'width="100" height="100">\n'
        f"  {extra_content}\n"
        "</svg>\n"
    )


def _minimal_1x1_png() -> bytes:
    """Build a valid 1x1 grayscale PNG (color type 0, 8-bit)."""
    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        import struct as _s
        length = _s.pack(">I", len(data))
        crc = _s.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    # IHDR: 1x1, 8-bit, grayscale (color type 0)
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    # IDAT: filter byte 0 + one gray pixel (value 128)
    raw = bytes([0, 128])  # filter=None, pixel=128
    idat = _chunk(b"IDAT", zlib.compress(raw))

    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


# ---------------------------------------------------------------------------
# Tests: prepare_svg_for_emboss
# ---------------------------------------------------------------------------

class TestPrepareSvgForEmboss:
    def test_basic_svg(self, tmp_path):
        from kiln.image_to_surface import prepare_svg_for_emboss

        svg_file = tmp_path / "test.svg"
        svg_file.write_text(_minimal_svg('<rect x="10" y="10" width="80" height="80" fill="black"/>'))

        result = prepare_svg_for_emboss(str(svg_file), str(tmp_path / "out"))

        assert result["type"] == "svg"
        assert result["width"] == 100.0
        assert result["height"] == 100.0
        assert result["aspect_ratio"] == 1.0
        assert os.path.isfile(result["svg_path"])

    def test_file_not_found(self, tmp_path):
        from kiln.image_to_surface import prepare_svg_for_emboss

        with pytest.raises(FileNotFoundError):
            prepare_svg_for_emboss(str(tmp_path / "nope.svg"), str(tmp_path / "out"))


class TestStrokeToFillConversion:
    def test_line_becomes_polygon(self, tmp_path):
        from kiln.image_to_surface import prepare_svg_for_emboss

        svg_content = _minimal_svg(
            '<line x1="10" y1="10" x2="90" y2="90" stroke="black" stroke-width="2"/>'
        )
        svg_file = tmp_path / "stroke.svg"
        svg_file.write_text(svg_content)

        result = prepare_svg_for_emboss(str(svg_file), str(tmp_path / "out"))

        # The processed SVG should contain a <polygon> element
        with open(result["svg_path"]) as f:
            processed = f.read()
        assert "<polygon" in processed


# ---------------------------------------------------------------------------
# Tests: generate_text_image
# ---------------------------------------------------------------------------

class TestGenerateTextImage:
    def test_returns_correct_type(self, tmp_path):
        from kiln.image_to_surface import generate_text_image

        result = generate_text_image("Hello", str(tmp_path))
        assert result["type"] == "openscad_text"
        assert result["text"] == "Hello"
        # No baked font_size by default: the emboss generator MEASURES the
        # rendered text and sizes it to the face (the old baked 48 rendered
        # "KILN" 146mm wide — off both edges of a 90mm coaster).
        assert "font_size" not in result

    def test_explicit_font_size_is_kept(self, tmp_path):
        from kiln.image_to_surface import generate_text_image

        result = generate_text_image("Hello", str(tmp_path), font_size=17)
        assert result["font_size"] == 17

    def test_returns_fragment(self, tmp_path):
        from kiln.image_to_surface import generate_text_image

        result = generate_text_image("Test", str(tmp_path))
        assert "openscad_fragment" in result
        assert "text(" in result["openscad_fragment"]
        assert "Test" in result["openscad_fragment"]


# ---------------------------------------------------------------------------
# Tests: generate_qr_data
# ---------------------------------------------------------------------------

class TestGenerateQrData:
    def test_raises_import_error(self, tmp_path):
        from kiln.image_to_surface import generate_qr_data

        with pytest.raises(ImportError, match="Pro feature"):
            generate_qr_data("https://kiln3d.com", str(tmp_path))


# ---------------------------------------------------------------------------
# Tests: prepare_image_for_emboss
# ---------------------------------------------------------------------------

def _grayscale_png(width: int, height: int, pixel_value: int = 128) -> bytes:
    """Build a valid WxH grayscale PNG with uniform pixel value."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        import struct as _s
        length = _s.pack(">I", len(data))
        crc = _s.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    raw_rows = b""
    for _ in range(height):
        raw_rows += bytes([0] + [pixel_value] * width)  # filter=None + pixels
    idat = _chunk(b"IDAT", zlib.compress(raw_rows))
    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


def _gradient_png(size: int = 50) -> bytes:
    """Build a grayscale PNG with a vertical gradient (dark top → light bottom)."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        import struct as _s
        length = _s.pack(">I", len(data))
        crc = _s.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    ihdr_data = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    raw_rows = b""
    for y in range(size):
        val = int(y * 255 / max(size - 1, 1))
        raw_rows += bytes([0] + [val] * size)
    idat = _chunk(b"IDAT", zlib.compress(raw_rows))
    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


class TestPrepareImageForEmboss:
    def test_minimal_png(self, tmp_path):
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "test.png"
        png_file.write_bytes(_minimal_1x1_png())

        result = prepare_image_for_emboss(str(png_file), str(tmp_path / "out"))

        assert result["type"] == "heightmap"
        assert result["width_px"] >= 1
        assert result["height_px"] >= 1
        assert "dat_path" in result
        assert os.path.isfile(result["dat_path"])


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("rembg"),
    reason="rembg not installed — coin pipeline requires background removal",
)
class TestCoinStyle:
    """Test the proven coin-relief pipeline (v11 Ash coaster)."""

    def test_coin_produces_dat_file(self, tmp_path):
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "photo.png"
        png_file.write_bytes(_gradient_png(50))

        result = prepare_image_for_emboss(
            str(png_file), str(tmp_path / "out"),
            style="coin", max_resolution=30,
        )

        assert result["type"] == "heightmap"
        assert os.path.isfile(result["dat_path"])
        assert result["width_px"] <= 30
        assert result["height_px"] <= 30

    def test_coin_8_level_posterize(self, tmp_path):
        """Coin style should produce at most 8 distinct gray levels."""
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "gradient.png"
        png_file.write_bytes(_gradient_png(50))

        result = prepare_image_for_emboss(
            str(png_file), str(tmp_path / "out"),
            style="coin", max_resolution=30,
        )

        # Read the DAT file and count distinct values
        with open(result["dat_path"]) as f:
            values = set()
            for line in f:
                for v in line.strip().split():
                    values.add(float(v))

        # 8-level posterize → at most 8 distinct values (plus 0.0 from mask)
        assert len(values) <= 9, f"Expected ≤9 distinct values, got {len(values)}: {sorted(values)}"

    def test_coin_circular_mask(self, tmp_path):
        """Coin style applies circular mask — corners should be zero."""
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "white.png"
        png_file.write_bytes(_grayscale_png(50, 50, pixel_value=200))

        result = prepare_image_for_emboss(
            str(png_file), str(tmp_path / "out"),
            style="coin", max_resolution=30,
        )

        with open(result["dat_path"]) as f:
            rows = [line.strip().split() for line in f if line.strip()]

        # Top-left corner (0,0) should be masked to 0
        assert float(rows[0][0]) == 0.0, "Corner should be masked to zero"
        # Bottom-right corner should also be masked
        assert float(rows[-1][-1]) == 0.0, "Corner should be masked to zero"


class TestFlipRows:
    """Test the flip_rows parameter for OpenSCAD surface() orientation."""

    def test_flip_rows_reverses_output(self, tmp_path):
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "gradient.png"
        png_file.write_bytes(_gradient_png(20))
        out_dir = str(tmp_path / "out")

        result_normal = prepare_image_for_emboss(
            str(png_file), out_dir, max_resolution=10,
        )
        with open(result_normal["dat_path"]) as f:
            rows_normal = [line.strip() for line in f if line.strip()]

        # Clean output dir for second run
        out_dir2 = str(tmp_path / "out2")
        result_flipped = prepare_image_for_emboss(
            str(png_file), out_dir2, max_resolution=10, flip_rows=True,
        )
        with open(result_flipped["dat_path"]) as f:
            rows_flipped = [line.strip() for line in f if line.strip()]

        assert len(rows_normal) == len(rows_flipped)
        assert rows_normal == list(reversed(rows_flipped))

    def test_flip_rows_default_false(self, tmp_path):
        """Default flip_rows=False should not reverse rows."""
        from kiln.image_to_surface import prepare_image_for_emboss

        png_file = tmp_path / "gradient.png"
        png_file.write_bytes(_gradient_png(20))

        # Two runs with default (False) should produce identical output
        r1 = prepare_image_for_emboss(
            str(png_file), str(tmp_path / "out1"), max_resolution=10,
        )
        r2 = prepare_image_for_emboss(
            str(png_file), str(tmp_path / "out2"), max_resolution=10, flip_rows=False,
        )

        with open(r1["dat_path"]) as f:
            d1 = f.read()
        with open(r2["dat_path"]) as f:
            d2 = f.read()
        assert d1 == d2
