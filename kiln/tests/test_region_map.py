"""Tests for the shared region-map door.

A region map's colors are ID labels.  Rendered like a product preview it
becomes a filament proposal in the reader's head — and the JSON that says
otherwise does not travel with the image.  These tests pin the parts of
the treatment that stop that: what the palette is allowed to look like,
what chrome is burned into the pixels, and that the image identifies its
own regions when it is the only thing left.

Coverage areas:
    - the palette is muted where a label-ID palette would be lurid
    - header and footer strips exist, and carry the correction verbatim
    - no ground-plane gradient, no product shading
    - numbered callouts land on the regions they name
    - the legend is drawn from the same palette the pixels use
    - regions past the legend cap are counted, not silently dropped
    - the brand rail: Kiln's mark in the header, the file name beside the title
    - callouts never overlap, and every visible region gets one
    - the legend reads in id order
    - refusals for an empty mesh and a mismatched face map
"""

from __future__ import annotations

import colorsys
import math
from pathlib import Path

import pytest

from kiln.colored_renderer import render_colored_mesh
from kiln.region_map import (
    _CALLOUT_FILL,
    _CALLOUT_GAP_PX,
    _EMBER,
    _FOOTER_DISCLAIMER,
    _HEADER_BG,
    _HEADER_H,
    _LEGEND_W,
    _MAX_LEGEND_ROWS,
    REGION_MAP_DISCLAIMER,
    _spread_callouts,
    region_palette,
    render_region_map,
)
from kiln.threemf_parser import ColoredTriangle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cylinder_with_bands(
    *, sections: int = 48, bands: int = 4, radius: float = 20.0, height: float = 50.0
) -> tuple[list[tuple], list[int]]:
    """A capped cylinder split into ``bands`` horizontal regions.

    Returns a triangle soup and a region id per triangle — the shape of
    input a segmenter hands the map renderer.
    """
    tris: list[tuple] = []
    face_region: list[int] = []
    edges = [height * i / bands for i in range(bands + 1)]
    for k in range(sections):
        a0 = 2 * math.pi * k / sections
        a1 = 2 * math.pi * (k + 1) / sections
        x0, y0 = radius * math.cos(a0), radius * math.sin(a0)
        x1, y1 = radius * math.cos(a1), radius * math.sin(a1)
        for b in range(bands):
            zb, zt = edges[b], edges[b + 1]
            tris.append(((x0, y0, zb), (x1, y1, zb), (x1, y1, zt)))
            tris.append(((x0, y0, zb), (x1, y1, zt), (x0, y0, zt)))
            face_region.extend([b, b])
        tris.append(((0.0, 0.0, 0.0), (x1, y1, 0.0), (x0, y0, 0.0)))
        face_region.append(bands)  # bottom cap: its own region, facing away
        tris.append(((0.0, 0.0, height), (x0, y0, height), (x1, y1, height)))
        face_region.append(bands + 1)  # top cap
    return tris, face_region


@pytest.fixture
def banded() -> tuple[list[tuple], list[int]]:
    return _cylinder_with_bands()


def _chroma(rgb: tuple[int, int, int]) -> int:
    return max(rgb) - min(rgb)


def _colors_of(img) -> list[tuple[int, tuple[int, int, int]]]:  # noqa: ANN001
    """Every distinct color in an RGB image with its pixel count."""
    return img.getcolors(1 << 24) or []


def _count_of(img, rgb: tuple[int, int, int]) -> int:  # noqa: ANN001
    return sum(n for n, c in _colors_of(img) if c == rgb)


def _legacy_id_palette(count: int) -> list[tuple[int, int, int]]:
    """The palette shape a region map gets when nobody thinks about it.

    A plain "make the ids far apart" hue walk at full saturation — the
    control this module's palette has to beat.  Not imported from
    anywhere: it is here to prove the metric below can fail.
    """
    out = []
    for i in range(count):
        hue = (i * 0.618033988749895) % 1.0
        sat = 0.75 if i % 2 == 0 else 0.95
        val = 1.0 if i % 3 != 2 else 0.72
        r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


class TestRegionPalette:
    """Label colors must not be able to pass for filament colors."""

    def test_muted_where_an_id_palette_is_lurid(self) -> None:
        mine = region_palette(12)
        control = _legacy_id_palette(12)

        # The twin: the obvious palette really is saturated, so the
        # metric is capable of failing.
        assert max(_chroma(c) for c in control) > 180

        assert max(_chroma(c) for c in mine) <= 90, (
            "a region label rendered at filament chroma reads as a "
            f"filament choice: {[c for c in mine if _chroma(c) > 90]}"
        )

    def test_stays_light_enough_to_carry_a_dark_callout(self) -> None:
        # Numbered discs are dark-on-light; a near-black fill under one
        # would swallow the number that identifies the region.
        assert all(sum(c) / 3 >= 90 for c in region_palette(24))

    def test_covers_every_id_and_survives_a_degenerate_count(self) -> None:
        assert len(region_palette(7)) == 7
        assert len(region_palette(0)) == 1
        assert len(region_palette(-3)) == 1


# ---------------------------------------------------------------------------
# The burned-in chrome
# ---------------------------------------------------------------------------


class TestBurnedInChrome:
    """The correction has to be in the pixels — the JSON does not travel."""

    def test_header_and_footer_strips_exist(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        render_region_map(tris, regions, output_path=out, width=700, height=560)

        with Image.open(out) as img:
            # A solid dark bar across the top.
            top = [img.getpixel((x, 3))[:3] for x in range(0, img.width, 7)]
            assert set(top) == {_HEADER_BG}
            # Text below it, in the same bar.
            band = [img.getpixel((x, 20))[:3] for x in range(10, 300)]
            assert len(set(band)) > 1, "the header bar is blank — no title drawn"
            # And a footer strip that is neither paper nor the header.
            foot = img.getpixel((5, img.height - 5))[:3]
            assert foot != _HEADER_BG
            assert foot != img.getpixel((img.width // 3, _HEADER_H + 5))[:3]

    def test_disclaimer_text_is_drawn_at_both_ends(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PIL import ImageDraw

        drawn: list[str] = []
        original = ImageDraw.ImageDraw.text

        def _record(self, xy, text, *args, **kwargs):  # noqa: ANN001, ANN202
            drawn.append(str(text))
            return original(self, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", _record)

        tris, regions = banded
        render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=700, height=560, note="Reference only.",
        )

        assert REGION_MAP_DISCLAIMER in drawn, (
            "the header must say what the colors are; an image that "
            "travels without its JSON has nothing else to go on"
        )
        assert any(_FOOTER_DISCLAIMER in t for t in drawn), (
            "the footer must repeat it — a crop of the top half is a "
            "region map with no correction on it"
        )
        assert any("Reference only." in t for t in drawn)

    def test_caller_note_cannot_replace_the_disclaimer(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PIL import ImageDraw

        drawn: list[str] = []
        original = ImageDraw.ImageDraw.text
        monkeypatch.setattr(
            ImageDraw.ImageDraw,
            "text",
            lambda self, xy, text, *a, **k: (
                drawn.append(str(text)), original(self, xy, text, *a, **k)
            )[1],
        )
        tris, regions = banded
        render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=700, height=560, title="Zone breakdown", note="",
        )
        assert REGION_MAP_DISCLAIMER in drawn
        assert "ZONE BREAKDOWN" in drawn


# ---------------------------------------------------------------------------
# The field itself
# ---------------------------------------------------------------------------


class TestDiagramField:
    """No ground plane, no product shading, no studio backdrop."""

    def test_field_has_no_background_gradient(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        render_region_map(tris, regions, output_path=out, width=700, height=560)

        with Image.open(out) as img:
            field_x = 8
            near_top = img.getpixel((field_x, _HEADER_H + 6))[:3]
            near_bottom = img.getpixel((field_x, img.height - 60))[:3]
        assert near_top == near_bottom, (
            "a vertical ramp reads as ambient light on a ground plane"
        )

        # The twin: the product path this used to go through DOES ramp.
        shaded = [
            ColoredTriangle(v0=t[0], v1=t[1], v2=t[2], color=(200, 60, 60))
            for t in tris
        ]
        product = str(tmp_path / "product.png")
        render_colored_mesh(shaded, output_path=product, width=400, height=400)
        with Image.open(product) as img:
            assert img.getpixel((4, 4))[:3] != img.getpixel((4, img.height - 5))[:3]

    def test_no_pixel_is_painted_at_filament_chroma(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        render_region_map(tris, regions, output_path=out, width=700, height=560)

        with Image.open(out) as img:
            field = img.crop(
                (0, _HEADER_H, img.width - _LEGEND_W, img.height - 40)
            ).convert("RGB")
            worst = max(_chroma(c) for _n, c in _colors_of(field))
        assert worst <= 110, f"a fill reached chroma {worst} — that is product color"


# ---------------------------------------------------------------------------
# Identification that survives the image travelling alone
# ---------------------------------------------------------------------------


class TestSelfIdentification:
    """Numbers on the object and a legend beside it, both in the pixels."""

    def test_callouts_are_drawn_for_the_visible_regions(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        result = render_region_map(
            tris, regions, output_path=out, width=700, height=560
        )
        assert result.labeled_ids, "nothing on the image identifies a region"

        with Image.open(out) as img:
            field = img.crop(
                (0, _HEADER_H, img.width - _LEGEND_W, img.height - 40)
            ).convert("RGB")
            discs = _count_of(field, _CALLOUT_FILL)
        # Each disc is a filled circle of radius >= 11.
        assert discs > 150 * len(result.labeled_ids)

        # The twin: the bare 3D field carries no such marks.  Not exactly
        # zero — antialiasing lands a handful of pixels on any given
        # value — but two orders of magnitude below one disc.
        shaded = [
            ColoredTriangle(v0=t[0], v1=t[1], v2=t[2], color=(163, 184, 204))
            for t in tris
        ]
        plain = str(tmp_path / "plain.png")
        render_colored_mesh(
            shaded, output_path=plain, width=400, height=400,
            background=(240, 240, 236), shading="matte", background_gradient=False,
        )
        with Image.open(plain) as img:
            assert _count_of(img.convert("RGB"), _CALLOUT_FILL) < 20

    def test_a_region_facing_away_is_named_but_not_pointed_at(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        tris, regions = banded
        result = render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=700, height=560, elevation=35.0,
        )
        bottom_cap = 4  # `bands`, the down-facing region
        assert bottom_cap not in result.labeled_ids
        assert result.region_count == 6

    def test_legend_swatches_use_the_palette_the_pixels_use(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        result = render_region_map(
            tris, regions, output_path=out, width=700, height=560
        )
        # One source of truth, not a second list that can drift.
        assert result.palette == region_palette(result.region_count)

        with Image.open(out) as img:
            legend = img.crop(
                (img.width - _LEGEND_W, _HEADER_H, img.width, img.height - 40)
            ).convert("RGB")
            swatch_colors = {c for _n, c in _colors_of(legend)}
        # Swatches are drawn unshaded, so the exact palette color appears.
        for rgb in result.palette[:4]:
            assert rgb in swatch_colors, f"legend never shows region color {rgb}"

    def test_regions_past_the_legend_cap_are_counted_not_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PIL import ImageDraw

        drawn: list[str] = []
        original = ImageDraw.ImageDraw.text
        monkeypatch.setattr(
            ImageDraw.ImageDraw,
            "text",
            lambda self, xy, text, *a, **k: (
                drawn.append(str(text)), original(self, xy, text, *a, **k)
            )[1],
        )
        tris, regions = _cylinder_with_bands(bands=30)
        result = render_region_map(
            tris, regions, output_path=str(tmp_path / "many.png"),
            width=700, height=560,
        )
        assert result.region_count == 32
        assert any("smaller region" in t for t in drawn)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_empty_mesh(self) -> None:
        with pytest.raises(ValueError, match="No triangles"):
            render_region_map([], [])

    def test_face_region_length_mismatch(
        self, banded: tuple[list[tuple], list[int]]
    ) -> None:
        tris, regions = banded
        with pytest.raises(ValueError, match="face_region has"):
            render_region_map(tris, regions[:-1])


# ---------------------------------------------------------------------------
# Brand rail, callout spacing, legend order
# ---------------------------------------------------------------------------


def _drawn_texts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every string handed to PIL's text(), in draw order."""
    from PIL import ImageDraw

    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def _record(self, xy, text, *args, **kwargs):  # noqa: ANN001, ANN202
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", _record)
    return drawn


def _cylinder_with_edges(edges: list[float], *, sections: int = 48, radius: float = 20.0):
    """Like ``_cylinder_with_bands`` but with caller-chosen band edges, so
    the bands can have deliberately unequal areas."""
    tris: list[tuple] = []
    face_region: list[int] = []
    bands = len(edges) - 1
    for k in range(sections):
        a0 = 2 * math.pi * k / sections
        a1 = 2 * math.pi * (k + 1) / sections
        x0, y0 = radius * math.cos(a0), radius * math.sin(a0)
        x1, y1 = radius * math.cos(a1), radius * math.sin(a1)
        for b in range(bands):
            zb, zt = edges[b], edges[b + 1]
            tris.append(((x0, y0, zb), (x1, y1, zb), (x1, y1, zt)))
            tris.append(((x0, y0, zb), (x1, y1, zt), (x0, y0, zt)))
            face_region.extend([b, b])
        tris.append(((0.0, 0.0, edges[0]), (x1, y1, edges[0]), (x0, y0, edges[0])))
        face_region.append(bands)
        tris.append(((0.0, 0.0, edges[-1]), (x0, y0, edges[-1]), (x1, y1, edges[-1])))
        face_region.append(bands + 1)
    return tris, face_region


class TestBrandRail:
    """The sheet is a Kiln document, and says which object it maps."""

    def test_the_ember_mark_is_in_the_rail_and_nowhere_else(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path
    ) -> None:
        from PIL import Image

        tris, regions = banded
        out = str(tmp_path / "map.png")
        render_region_map(tris, regions, output_path=out, width=700, height=560)
        with Image.open(out).convert("RGB") as img:
            rail = img.crop((0, 0, img.width, _HEADER_H))
            sheet = img.crop((0, _HEADER_H, img.width, img.height))
            assert _count_of(rail, _EMBER) > 20, "no Kiln mark in the header"
            assert _count_of(sheet, _EMBER) == 0, (
                "brand orange below the rail would read as a filament color"
            )

    def test_wordmark_and_subject_are_drawn(
        self, banded: tuple[list[tuple], list[int]], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        drawn = _drawn_texts(monkeypatch)
        tris, regions = banded
        render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=700, height=560, subject="bracket_v3.stl",
        )
        # K·I·L·N is drawn letter by letter so the I can wear the ember.
        runs = [tuple(drawn[i : i + 4]) for i in range(len(drawn) - 3)]
        assert ("K", "I", "L", "N") in runs, drawn[:12]
        assert "bracket_v3.stl" in drawn
        assert "kiln3d.com" in drawn


class TestCallouts:
    """Every visible region gets a number, and no number hides another."""

    def test_coincident_anchors_are_pushed_apart(self) -> None:
        field = (0, 0, 400, 300)
        anchors = {0: (200.0, 150.0), 1: (200.0, 150.0), 2: (203.0, 149.0)}
        radii = {0: 12, 1: 12, 2: 14}
        placed = _spread_callouts(anchors, radii, field)
        ids = list(placed)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                d = math.hypot(placed[a][0] - placed[b][0], placed[a][1] - placed[b][1])
                assert d >= radii[a] + radii[b] + _CALLOUT_GAP_PX - 1, (a, b, d)
        for rid, (x, y) in placed.items():
            r = radii[rid]
            assert r <= x <= 400 - r and r <= y <= 300 - r

    def test_spreading_is_deterministic(self) -> None:
        field = (0, 0, 400, 300)
        anchors = {0: (100.0, 100.0), 1: (100.0, 100.0)}
        radii = {0: 11, 1: 11}
        assert _spread_callouts(anchors, radii, field) == _spread_callouts(
            anchors, radii, field
        )

    def test_a_visible_region_past_the_legend_cap_still_gets_a_number(
        self, tmp_path: Path
    ) -> None:
        # More side bands than the legend has rows, every one of them in
        # view: the legend truncates, the picture must not.
        bands = _MAX_LEGEND_ROWS + 4
        edges = [60.0 * i / bands for i in range(bands + 1)]
        tris, regions = _cylinder_with_edges(edges)
        result = render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=900, height=900, elevation=10.0,
        )
        assert len(result.labeled_ids) > _MAX_LEGEND_ROWS, (
            "a colored patch with no callout is a region the reader cannot name"
        )


class TestLegendOrder:
    def test_rows_read_in_id_order_even_when_area_says_otherwise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Band 0 is the thinnest, band 3 the tallest: by area the legend
        # would open with Region 3.  The ids are what a caller types into
        # a paint request, so the column counts upward instead.
        tris, regions = _cylinder_with_edges([0.0, 3.0, 8.0, 18.0, 60.0])
        drawn = _drawn_texts(monkeypatch)
        render_region_map(
            tris, regions, output_path=str(tmp_path / "map.png"),
            width=700, height=560,
        )
        rows = [t for t in drawn if t.startswith("Region ")]
        ids = [int(t.split()[1]) for t in rows]
        assert ids == sorted(ids), rows
        assert ids[0] == 0
