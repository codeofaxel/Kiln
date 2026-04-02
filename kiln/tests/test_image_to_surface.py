"""Tests for kiln.image_to_surface module."""

from __future__ import annotations

import os
import struct
import tempfile
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
        with open(result["svg_path"], "r") as f:
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
        assert result["font_size"] == 48

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
