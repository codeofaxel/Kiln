"""Tests for kiln.image_to_surface module."""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

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

        # The stroke must become carvable filled geometry.  The mark
        # parser now expands it directly to native OpenSCAD polygons
        # (previously: a rewritten SVG file with <polygon> elements).
        assert "polygon(" in result["openscad_polygons"]
        assert result["content_width"] > 0


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
    def test_names_the_tier_that_actually_includes_it(self, tmp_path):
        """The stub refuses, and must name the RIGHT tier.

        It said "Pro feature" while every gate in kiln-pro checks
        Business — so a Pro customer could read this, upgrade to Pro,
        and still not have QR codes.  A refusal that misdirects the
        upgrade is worse than a refusal.
        """
        from kiln.image_to_surface import generate_qr_data

        with pytest.raises(ImportError, match="Business feature"):
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

        # A 1x1 image is one flat tone: there is nothing in it to carve, and
        # the engine says so instead of writing a heightmap of nothing.
        png_file = tmp_path / "test.png"
        png_file.write_bytes(_minimal_1x1_png())

        with pytest.raises(ValueError, match="no visible content"):
            prepare_image_for_emboss(str(png_file), str(tmp_path / "out"))

    def test_smallest_image_with_content(self, tmp_path):
        from PIL import Image

        from kiln.image_to_surface import prepare_image_for_emboss

        img = Image.new("L", (2, 2), 255)
        img.putpixel((0, 0), 0)
        png_file = tmp_path / "two_tone.png"
        img.save(png_file)

        result = prepare_image_for_emboss(str(png_file), str(tmp_path / "out"))

        assert result["type"] == "heightmap"
        assert result["width_px"] >= 1
        assert result["height_px"] >= 1
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


class TestAlphaFlattening:
    """Transparency must decode as empty field (white), never as ink (black).

    ``convert("L")`` silently drops the alpha channel, and a dropped alpha
    decodes a transparent surround as black — which the emboss pipeline
    reads as maximum displacement.  Every Pillow mode that can carry
    transparency goes through ``_flatten_alpha_on_white``; these tests pin
    each mode at the loader, where every door in the module meets it.
    """

    def _corners(self, rows):
        return [rows[0][0], rows[0][-1], rows[-1][0], rows[-1][-1]]

    def test_rgba_transparent_surround_reads_as_white(self, tmp_path):
        from PIL import Image, ImageDraw

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle(
            [15, 15, 45, 45], outline=(0, 0, 0, 255), width=4
        )
        p = tmp_path / "rgba.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert self._corners(rows) == [255, 255, 255, 255]
        assert min(rows[30]) == 0  # the ink itself still reads as ink

    def test_la_mode_transparent_surround_reads_as_white(self, tmp_path):
        from PIL import Image, ImageDraw

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("LA", (60, 60), (0, 0))
        ImageDraw.Draw(img).rectangle(
            [15, 15, 45, 45], outline=(0, 255), width=4
        )
        p = tmp_path / "la.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert self._corners(rows) == [255, 255, 255, 255]

    def test_palette_transparency_reads_as_white(self, tmp_path):
        from PIL import Image, ImageDraw

        from kiln.image_to_surface import _load_image_as_grayscale

        rgba = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(rgba).rectangle(
            [15, 15, 45, 45], outline=(0, 0, 0, 255), width=4
        )
        p = tmp_path / "palette.png"
        rgba.convert("P").save(p)

        # The saved file must actually carry palette transparency for this
        # test to test anything.
        reopened = Image.open(p)
        assert reopened.mode == "P"
        assert "transparency" in reopened.info

        rows, w, h = _load_image_as_grayscale(str(p))
        assert self._corners(rows) == [255, 255, 255, 255]

    def test_opaque_image_is_untouched(self, tmp_path):
        from PIL import Image

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("L", (20, 20), 77)
        p = tmp_path / "opaque.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 77
        assert rows[-1][-1] == 77

    def test_grayscale_trns_transparency_reads_as_white(self, tmp_path):
        """An L-mode PNG can mark a tone transparent via tRNS — no alpha band."""
        from PIL import Image

        from kiln.image_to_surface import _load_image_as_grayscale

        p = tmp_path / "l_trns.png"
        Image.new("L", (20, 20), 0).save(p, transparency=0)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255

    def test_rgb_trns_transparency_reads_as_white(self, tmp_path):
        from PIL import Image

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGB", (20, 20), (0, 255, 0))
        for y in range(8, 12):
            for x in range(8, 12):
                img.putpixel((x, y), (40, 40, 40))
        p = tmp_path / "rgb_trns.png"
        img.save(p, transparency=(0, 255, 0))

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255
        # One flat tone on a transparent surround is artwork: the coverage
        # is the ink, at full depth, whatever colour it was drawn in.
        assert rows[10][10] == 0

    def test_white_ink_on_transparency_survives_as_alpha_ink(self, tmp_path):
        """A white mark on a transparent surround must not vanish.

        Flattening onto white erases it — the alpha channel is the only
        place that mark exists, so the alpha coverage becomes the ink.
        """
        from PIL import Image, ImageDraw

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle(
            [20, 20, 40, 40], fill=(255, 255, 255, 255)
        )
        p = tmp_path / "white_ink.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255, "surround must stay empty field"
        assert rows[30][30] == 0, (
            "the white mark vanished — the alpha-as-ink fallback is gone"
        )

    def test_flat_grey_mark_on_transparency_carves_at_full_depth(self, tmp_path):
        """A mark's colour is identity, not depth.

        A mid-grey logo used to carve at half the depth that was asked
        for, because luminance was read as relief.  Flat artwork on a
        transparent surround now carves from its alpha coverage.
        """
        from PIL import Image, ImageDraw

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle(
            [20, 20, 40, 40], fill=(128, 128, 128, 255)
        )
        p = tmp_path / "gray_ink.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255
        assert rows[30][30] == 0, "a grey mark must carve like a black one"

    @staticmethod
    def _two_tone_logo():
        """White strokes plus an orange accent on a transparent field —
        the light variant of a brand kit, which is what the Kiln logo is."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([10, 10, 70, 30], outline=(255, 255, 255, 255), width=3)
        d.line([12, 45, 68, 45], fill=(255, 100, 40, 255), width=3)
        d.rectangle([36, 55, 44, 70], fill=(255, 255, 255, 255))
        return img

    def test_white_strokes_beside_an_orange_accent_are_still_the_mark(self, tmp_path):
        """The white part of a two-tone logo must not vanish.

        The old rescue only fired when essentially NO opaque content
        survived a white flatten.  A logo whose accent is visible but
        whose strokes are white lost its strokes and kept the accent —
        the Kiln mark came out as a faint ring and nothing else.
        """
        from kiln.image_to_surface import _load_image_as_grayscale

        p = tmp_path / "two_tone.png"
        self._two_tone_logo().save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255, "surround must stay empty field"
        assert rows[11][40] == 0, "the white stroke is part of the mark"
        assert rows[45][40] == 0, "the orange accent is part of the mark"
        assert rows[62][40] == 0, "the white glyph is part of the mark"

    def test_pure_python_decoder_reads_alpha_by_the_same_rule(self, tmp_path):
        """The no-Pillow PNG door must not disagree with the Pillow door."""
        from kiln.image_to_surface import _load_image_as_grayscale, _read_png_pixels

        p = tmp_path / "two_tone.png"
        self._two_tone_logo().save(p)

        via_pillow, _, _ = _load_image_as_grayscale(str(p))
        via_decoder, _, _ = _read_png_pixels(str(p))
        assert via_decoder[11][40] == 0, "white stroke lost by the pure-Python door"
        assert via_decoder == via_pillow

    def test_tonal_cutout_on_transparency_keeps_its_relief(self, tmp_path):
        """A photograph exported with alpha is relief, not a silhouette.

        Its tones are the carve; the alpha only says where the subject
        ends.  Compositing onto white keeps every tone and makes the
        cut-away surround empty field.
        """
        from PIL import Image

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        px = img.load()
        for y in range(16, 48):
            for x in range(8, 56):
                tone = int((x - 8) * 255 / 47)
                px[x, y] = (tone, tone, tone, 255)
        p = tmp_path / "cutout.png"
        img.save(p)

        rows, w, h = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255
        assert rows[32][10] < 40, "dark end of the gradient must stay dark"
        assert 100 < rows[32][32] < 160, "mid-tones must survive"
        assert rows[32][54] > 220, "light end of the gradient must stay light"


_KILN_LOGO = Path(__file__).resolve().parents[2] / "docs" / "assets" / "kiln-logo-transparent.png"


@pytest.mark.skipif(not _KILN_LOGO.is_file(), reason="repo logo asset not present")
class TestRealLogoThroughAProductProfile:
    """The Kiln logo through the exact call a product profile makes.

    The profiles ask for photo relief (``style="coin"`` at 300 px, deboss).
    The logo is white and orange line art on a transparent field, and it
    came out as a faint ring: the white strokes vanished into the white
    flatten and the rest was posterised into the background.
    """

    def test_coin_profile_carves_only_the_mark(self, tmp_path):
        from kiln.image_to_surface import prepare_image_for_emboss

        info = prepare_image_for_emboss(
            str(_KILN_LOGO),
            str(tmp_path),
            max_resolution=300,
            invert=True,
            style="coin",
            flip_rows=True,
        )
        assert info["treatment"] == "mark"

        values = [
            float(v)
            for line in Path(info["dat_path"]).read_text().splitlines()
            if line.strip() and not line.startswith("#")
            for v in line.split()
        ]
        # Deboss convention: 1.0 is the untouched field, 0.0 is the full cut.
        w = info["width_px"]
        corners = [values[0], values[w - 1], values[-w], values[-1]]
        assert corners == [1.0] * 4, "the transparent surround must stay flush"
        assert min(values) == 0.0, "the mark must reach the full requested depth"
        carved = sum(1 for v in values if v < 0.98) / len(values)
        assert 0.002 < carved < 0.05, (
            f"{carved:.1%} of the face is cut — the mark is thin line art on "
            "an empty field, so only a few percent may move"
        )


def _dat_values(info):
    return [
        float(v)
        for line in Path(info["dat_path"]).read_text().splitlines()
        if line.strip() and not line.startswith("#")
        for v in line.split()
    ]


class TestAlphaEdgeCases:
    """The corners of the alpha rule, each one measured before it was pinned."""

    def test_fully_transparent_image_is_refused_not_carved(self, tmp_path):
        """A blank used to come out as a 70%-of-the-face pool with nothing in it."""
        from PIL import Image

        from kiln.image_to_surface import prepare_image_for_emboss

        p = tmp_path / "blank.png"
        Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(p)
        with pytest.raises(ValueError, match="no visible content"):
            prepare_image_for_emboss(
                str(p), str(tmp_path / "out"), max_resolution=200, invert=True,
                style="coin", flip_rows=True,
            )

    def test_faint_drop_shadow_is_not_the_mark(self, tmp_path):
        """An exporter's soft shadow around a logo carved as a halo."""
        from PIL import Image, ImageDraw, ImageFilter

        from kiln.image_to_surface import prepare_image_for_emboss

        img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle([34, 34, 84, 84], fill=(0, 0, 0, 100))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3)))
        ImageDraw.Draw(img).rectangle([30, 30, 80, 80], fill=(0, 0, 0, 255))
        p = tmp_path / "shadowed.png"
        img.save(p)

        info = prepare_image_for_emboss(
            str(p), str(tmp_path / "out"), max_resolution=120, invert=True,
            style="default", edge_enhance=False, flip_rows=True,
        )
        assert info["treatment"] == "mark"
        values = _dat_values(info)
        carved = sum(1 for v in values if v < 0.98) / len(values)
        square = 51 * 51 / (120 * 120)
        assert carved < square * 1.03, (
            f"{carved:.3f} of the face is cut for a mark covering {square:.3f} — "
            "the shadow is being carved"
        )

    def test_tiny_alpha_mark_is_still_a_mark(self, tmp_path):
        """The alpha channel already said it is a mark; a histogram probe
        that needs 8x8 pixels to be sure must not overrule it."""
        from PIL import Image

        from kiln.image_to_surface import prepare_image_for_emboss

        img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        img.putpixel((1, 1), (255, 255, 255, 255))
        p = tmp_path / "tiny.png"
        img.save(p)

        info = prepare_image_for_emboss(
            str(p), str(tmp_path / "out"), max_resolution=4, invert=True,
            style="coin", flip_rows=True,
        )
        assert info["treatment"] == "mark"
        values = _dat_values(info)
        assert values[0] == 1.0, "the transparent surround must stay flush"
        assert min(values) == 0.0, "the one opaque pixel is the whole mark"

    def test_low_contrast_photo_cutout_with_real_range_keeps_relief(self, tmp_path):
        """A gradient spanning a modest range is still tonal content.

        With the flat-art tone band twice as wide as it is now, four
        clusters covered 68 levels and a 60-level gradient was called
        flat artwork — carved as a silhouette, its relief thrown away.
        """
        from PIL import Image

        from kiln.image_to_surface import _load_image_as_grayscale

        img = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        px = img.load()
        for y in range(10, 70):
            for x in range(10, 70):
                tone = 80 + int((x - 10) * 60 / 59)  # 80..140
                px[x, y] = (tone, tone, tone, 255)
        p = tmp_path / "soft.png"
        img.save(p)

        rows, _, _ = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255
        assert 75 <= rows[40][12] <= 90, "the dark end must keep its tone"
        assert 130 <= rows[40][68] <= 145, "the light end must keep its tone"

    def test_grayscale_sixteen_bit_with_trns_carves_its_coverage(self, tmp_path):
        import numpy as np
        from PIL import Image

        from kiln.image_to_surface import _alpha_verdict, _load_image_as_grayscale

        arr = np.zeros((64, 64), dtype=np.uint16)
        arr[16:48, 16:48] = 65535
        p = tmp_path / "gray16.png"
        Image.fromarray(arr, mode="I;16").save(p, transparency=0)
        assert "transparency" in Image.open(p).info

        assert _alpha_verdict(str(p)) == "ink"
        rows, _, _ = _load_image_as_grayscale(str(p))
        assert rows[0][0] == 255
        assert rows[32][32] == 0


class TestPurePythonDecoderTransparencyModes:
    """The no-Pillow PNG door reads every way a PNG carries transparency."""

    def _agree(self, path):
        from kiln.image_to_surface import _load_image_as_grayscale, _read_png_pixels

        via_decoder, _, _ = _read_png_pixels(str(path))
        via_pillow, _, _ = _load_image_as_grayscale(str(path))
        assert via_decoder == via_pillow
        return via_decoder

    def test_palette_with_trns(self, tmp_path):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle([10, 10, 30, 30], fill=(255, 255, 255, 255))
        p = tmp_path / "pal.png"
        img.convert("P").save(p)
        rows = self._agree(p)
        assert rows[0][0] == 255 and rows[20][20] == 0

    def test_opaque_palette(self, tmp_path):
        from PIL import Image

        img = Image.new("RGB", (16, 16), (200, 200, 200))
        img.putpixel((3, 3), (10, 10, 10))
        p = tmp_path / "pal_opaque.png"
        img.convert("P").save(p)
        rows = self._agree(p)
        assert rows[3][3] < 30 and rows[0][0] > 190

    def test_grayscale_trns(self, tmp_path):
        from PIL import Image

        p = tmp_path / "l_trns.png"
        img = Image.new("L", (20, 20), 0)
        for y in range(6, 14):
            for x in range(6, 14):
                img.putpixel((x, y), 200)
        img.save(p, transparency=0)
        rows = self._agree(p)
        assert rows[0][0] == 255 and rows[10][10] == 0

    def test_rgb_trns(self, tmp_path):
        from PIL import Image

        img = Image.new("RGB", (20, 20), (0, 255, 0))
        for y in range(8, 12):
            for x in range(8, 12):
                img.putpixel((x, y), (40, 40, 40))
        p = tmp_path / "rgb_trns.png"
        img.save(p, transparency=(0, 255, 0))
        rows = self._agree(p)
        assert rows[0][0] == 255 and rows[10][10] == 0


class TestToneDecisionReadsTheInterior:
    """Anti-aliased edge pixels must not decide whether artwork is flat."""

    @staticmethod
    def _bleeding_white_logo(path):
        """A white mark whose soft edge blends toward BLACK as alpha falls —
        the premultiplied-looking export some tools write.  On line art the
        edge pixels rival the interior in number."""
        from PIL import Image, ImageDraw, ImageFilter

        alpha = Image.new("L", (160, 160), 0)
        d = ImageDraw.Draw(alpha)
        d.rectangle([20, 20, 140, 40], fill=255)
        d.rectangle([20, 60, 40, 140], fill=255)
        d.line([60, 140, 140, 60], fill=255, width=14)
        alpha = alpha.filter(ImageFilter.GaussianBlur(2.5))
        # colour = white * alpha  (bleeds to black where alpha thins)
        colour = alpha.point(lambda a: a)
        img = Image.merge("RGBA", (colour, colour, colour, alpha))
        img.save(path)

    def test_bleeding_edges_do_not_turn_a_logo_into_a_photo(self, tmp_path):
        from kiln.image_to_surface import _alpha_verdict, _load_image_as_grayscale

        p = tmp_path / "bleed.png"
        self._bleeding_white_logo(p)
        assert _alpha_verdict(str(p)) == "ink"
        rows, _, _ = _load_image_as_grayscale(str(p))
        assert rows[30][80] == 0, "the white bar must carve as the mark"
        assert rows[0][0] == 255

    def test_five_close_tones_are_relief_not_a_silhouette(self, tmp_path):
        """Five flat tones eight levels apart is a soft posterised shading —
        tonal content — not a logo in five brand colours.  A wider tone band
        swallowed two tones per window and called it flat artwork."""
        from PIL import Image

        from kiln.image_to_surface import _alpha_verdict, _load_image_as_grayscale

        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        px = img.load()
        for band, tone in enumerate((100, 108, 116, 124, 132)):
            for y in range(10, 90):
                for x in range(10 + band * 16, 26 + band * 16):
                    px[x, y] = (tone, tone, tone, 255)
        p = tmp_path / "five_tones.png"
        img.save(p)
        assert _alpha_verdict(str(p)) == "tones"
        rows, _, _ = _load_image_as_grayscale(str(p))
        assert rows[50][18] == 100 and rows[50][82] == 132
