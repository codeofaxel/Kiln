"""Region maps — the one door for "here is how this mesh is divided up".

A region map answers a question about STRUCTURE: which stretches of
surface the segmenter considers one piece.  The colors on it are ID
labels, chosen by counting, and they mean nothing about filament.

That is the whole reason this module exists.  Rendered through the plain
product path, a region map comes out as a red mug with a magenta handle
sitting in studio light — pixel-for-pixel the language Kiln uses for
"here is what your object will look like when it prints".  A user who
sees one reads a color proposal, checks it against the spools in the AMS,
and finds it wrong, because it was never about filament.  The JSON beside
it says so, but the JSON does not travel: the image gets pasted into a
chat, a message, a doc, and arrives with no caption at all.

So the disambiguation has to be in the pixels, and every caller has to
get it.  :func:`render_region_map` is that shared door — palette,
shading, chrome, callouts and legend in one place — and the treatment it
applies is deliberately un-product-like:

* matte, quantized shading with no rim or highlight (:mod:`kiln.colored_renderer`
  ``shading="matte"``),
* a flat paper field with no background gradient, so nothing implies a
  ground plane or a print bed,
* a muted drafting palette no one would mistake for a spool of filament,
* header and footer strips that say what the colors mean, burned into
  the image at both ends so a crop of either half still carries it,
* numbered callouts on the object plus a numbered legend, so the image
  identifies its own regions when it is all that is left.

Callers pass a triangle soup and a face-to-region mapping and get a PNG
back.  They must NOT re-derive the palette: :func:`region_palette` is the
same function this renderer uses, so a legend built from it always names
the color that is actually on screen.
"""

from __future__ import annotations

import colorsys
import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kiln._vec import Vec3

__all__ = [
    "REGION_MAP_DISCLAIMER",
    "RegionMapResult",
    "region_palette",
    "render_region_map",
]

# ---------------------------------------------------------------------------
# The sentence this whole module exists to put on the image
# ---------------------------------------------------------------------------

#: Burned into the header of every region map.  Short enough to survive a
#: thumbnail, explicit enough that nobody checks it against their spools.
REGION_MAP_DISCLAIMER = "Colors are region ID labels, not filament colors."

#: Burned into the footer, so a crop that loses the header still carries
#: the correction.
_FOOTER_DISCLAIMER = (
    "Diagram of how the surface divides. Filament colors are chosen separately."
)

_DEFAULT_TITLE = "REGION MAP"

# ---------------------------------------------------------------------------
# Document palette — a drafting sheet, not a studio
# ---------------------------------------------------------------------------

_PAPER = (240, 240, 236)
_INK = (26, 29, 34)
_HEADER_BG = (26, 29, 34)
_HEADER_FG = (246, 246, 243)
_HEADER_SUB = (183, 191, 203)
_FOOTER_BG = (222, 223, 218)
_FOOTER_FG = (74, 80, 88)
_RULE = (196, 199, 192)
_LEGEND_BG = (232, 233, 228)

_HEADER_H = 66
_FOOTER_H = 38
_LEGEND_W = 244

#: Callout disc fill.  Deliberately not the paper color: the disc has to
#: be findable in the image as its own mark.
_CALLOUT_FILL = (252, 252, 250)

#: A region smaller than this many rendered pixels gets no callout disc —
#: a number floating on a three-pixel sliver identifies nothing and
#: covers the sliver up.  It still appears in the legend.
_MIN_CALLOUT_PX = 130

#: Legend rows.  Past this the map says how many it left out rather than
#: growing a column nobody can read.
_MAX_LEGEND_ROWS = 12

_FONT_CANDIDATES_REGULAR = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)
_FONT_CANDIDATES_BOLD = (
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
)


def _font(size: int, *, bold: bool = False) -> Any:
    """Best available sans face at ``size``, falling back to PIL's bitmap."""
    from PIL import ImageFont

    for cand in _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR:
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Region palette
# ---------------------------------------------------------------------------


def region_palette(count: int) -> list[tuple[int, int, int]]:
    """Muted drafting colors, one per region id.

    A golden-ratio hue walk keeps sequential ids far apart on the wheel,
    but saturation and value stay in a deliberately washed-out band: a
    region map should not be able to pass for a photograph of a printed
    object, and high-chroma fills are most of what makes one look like
    it.  Nothing here is a filament color, so nothing here should look
    like one.

    Identification does not rest on telling these apart by eye — the
    numbered callouts and the legend do that — so the palette can afford
    to stay quiet.

    :param count: Number of regions.  Values below 1 yield one entry.
    :returns: RGB tuples indexed by region id.
    """
    palette: list[tuple[int, int, int]] = []
    for i in range(max(count, 1)):
        hue = (0.58 + i * 0.618033988749895) % 1.0
        sat = 0.20 if i % 2 == 0 else 0.30
        val = 0.80 if i % 3 != 2 else 0.63
        r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
        palette.append((int(r * 255), int(g * 255), int(b * 255)))
    return palette


def _hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class RegionMapResult:
    """A rendered region map and what it says about itself."""

    path: str
    width: int
    height: int
    region_count: int
    #: RGB per region id — the same list the pixels were painted from.
    #: A caller building a legend outside the image reads it from here
    #: rather than calling :func:`region_palette` again, so the two can
    #: never drift into naming different colors for the same region.
    palette: list[tuple[int, int, int]] = field(default_factory=list)
    #: Region ids that got a numbered callout on the image.
    labeled_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "region_count": self.region_count,
            "labeled_ids": list(self.labeled_ids),
            "legend_note": REGION_MAP_DISCLAIMER,
        }


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_region_map(
    triangles: Sequence[tuple[Vec3, Vec3, Vec3]],
    face_region: Sequence[int],
    *,
    output_path: str | None = None,
    width: int = 900,
    height: int = 700,
    elevation: float = 35.0,
    azimuth: float = 45.0,
    supersample: int = 2,
    region_count: int | None = None,
    region_area_fractions: Sequence[float] | None = None,
    title: str = _DEFAULT_TITLE,
    note: str = "",
) -> RegionMapResult:
    """Render a mesh's segmentation as a labelled diagram.

    Every caller that shows a user how a mesh divides into regions goes
    through here, so the "these are labels, not filament" treatment is
    applied once rather than remembered per caller.

    :param triangles: Triangle soup — one ``(v0, v1, v2)`` per face, in
        the same order as ``face_region``.
    :param face_region: Region id per triangle.
    :param output_path: Where to write the PNG.  Defaults to a temp file.
    :param width: Output image width, chrome included.
    :param height: Output image height, chrome included.
    :param elevation: Camera elevation in degrees.
    :param azimuth: Camera azimuth in degrees.
    :param supersample: Supersampling factor for the 3D pass.
    :param region_count: Total regions, when it exceeds the highest id
        present in ``face_region`` (regions can exist off-camera).
    :param region_area_fractions: Optional 0-1 surface-area share per
        region id, for the legend.  Omitted rather than guessed when the
        caller does not have it — the segmenter owns that number.
    :param title: Heading text.
    :param note: Extra footer sentence appended after the disclaimer.
        The disclaimer itself is not overridable; it is the point.
    :returns: :class:`RegionMapResult`.
    :raises ValueError: on an empty mesh or a ``face_region`` whose
        length does not match ``triangles``.
    """
    from PIL import Image, ImageDraw

    from kiln.colored_renderer import render_colored_mesh
    from kiln.threemf_parser import ColoredTriangle

    if not triangles:
        raise ValueError("No triangles to render")
    if len(face_region) != len(triangles):
        raise ValueError(
            f"face_region has {len(face_region)} entries for "
            f"{len(triangles)} triangles"
        )

    total_regions = max(region_count or 0, max(face_region) + 1)
    palette = region_palette(total_regions)

    if output_path is None:
        out_dir = os.path.join(tempfile.gettempdir(), "kiln_region_maps")
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        fd, output_path = tempfile.mkstemp(suffix=".png", dir=out_dir)
        os.close(fd)

    map_w = max(120, width - _LEGEND_W)
    map_h = max(120, height - _HEADER_H - _FOOTER_H)

    shaded = [
        ColoredTriangle(
            v0=tri[0], v1=tri[1], v2=tri[2], color=palette[face_region[i]]
        )
        for i, tri in enumerate(triangles)
    ]

    render = render_colored_mesh(
        shaded,
        output_path=os.path.join(
            os.path.dirname(output_path) or ".", "_region_map_field.png"
        ),
        width=map_w,
        height=map_h,
        elevation=elevation,
        azimuth=azimuth,
        background=_PAPER,
        supersample=supersample,
        # The two lines that stop this being a product render.
        shading="matte",
        background_gradient=False,
        # Ask the renderer where things landed instead of re-projecting.
        face_labels=list(face_region),
    )

    anchors = render.label_anchors or {}
    pixels = render.label_pixels or {}

    canvas = Image.new("RGB", (width, height), _PAPER)
    with Image.open(render.path) as field_img:
        canvas.paste(field_img.convert("RGB"), (0, _HEADER_H))
    with contextlib.suppress(OSError):
        os.unlink(render.path)

    draw = ImageDraw.Draw(canvas)

    # Which regions the image will identify by number: biggest first, by
    # the segmenter's area when it gave us one, else by what is actually
    # visible from this angle.
    ordered = sorted(
        range(total_regions),
        key=lambda rid: (
            -(region_area_fractions[rid] if region_area_fractions else 0.0),
            -pixels.get(rid, 0),
            rid,
        ),
    )
    legend_ids = ordered[:_MAX_LEGEND_ROWS]
    labeled_ids = [
        rid
        for rid in legend_ids
        if rid in anchors and pixels.get(rid, 0) >= _MIN_CALLOUT_PX
    ]

    # Why a legend row has no disc on the object.  A row the reader
    # cannot find in the picture and cannot be told why is worse than no
    # row, and "hidden" for a region that is on screen but three pixels
    # wide would be a plain untruth.
    statuses = {
        rid: (
            ""
            if rid in labeled_ids
            else ("not in this view" if rid not in anchors else "too small to label")
        )
        for rid in legend_ids
    }

    _draw_callouts(
        draw,
        labeled_ids,
        anchors,
        offset_y=_HEADER_H,
        field=(0, _HEADER_H, map_w, _HEADER_H + map_h),
    )
    _draw_header(draw, width, title=title, region_count=total_regions)
    _draw_legend(
        draw,
        canvas_w=width,
        canvas_h=height,
        palette=palette,
        legend_ids=legend_ids,
        statuses=statuses,
        total_regions=total_regions,
        area_fractions=region_area_fractions,
    )
    _draw_footer(draw, width, height, note=note)

    canvas.save(output_path, "PNG")
    return RegionMapResult(
        path=output_path,
        width=width,
        height=height,
        region_count=total_regions,
        palette=palette,
        labeled_ids=labeled_ids,
    )


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------


def _draw_header(
    draw: Any, width: int, *, title: str, region_count: int
) -> None:
    """Dark title bar: what this is, and what its colors are not."""
    draw.rectangle([0, 0, width, _HEADER_H - 1], fill=_HEADER_BG)
    draw.text((18, 12), title.upper(), fill=_HEADER_FG, font=_font(21, bold=True))
    draw.text(
        (18, 40), REGION_MAP_DISCLAIMER, fill=_HEADER_SUB, font=_font(13)
    )

    count_font = _font(13)
    count_text = f"{region_count} regions"
    tw = draw.textlength(count_text, font=count_font)
    draw.text((width - 18 - tw, 40), count_text, fill=_HEADER_SUB, font=count_font)


def _draw_footer(draw: Any, width: int, height: int, *, note: str) -> None:
    """The same correction again, at the other end of the image."""
    top = height - _FOOTER_H
    draw.rectangle([0, top, width, height], fill=_FOOTER_BG)
    draw.line([(0, top), (width, top)], fill=_RULE, width=1)
    text = _FOOTER_DISCLAIMER
    if note:
        text = f"{text}  {note}"
    draw.text((18, top + 12), text, fill=_FOOTER_FG, font=_font(12))


def _draw_callouts(
    draw: Any,
    labeled_ids: Sequence[int],
    anchors: dict[int, tuple[int, int]],
    *,
    offset_y: int,
    field: tuple[int, int, int, int],
) -> None:
    """Numbered discs on the object — the identification that survives a crop."""
    font = _font(13, bold=True)
    fx0, fy0, fx1, fy1 = field
    for rid in labeled_ids:
        x, y = anchors[rid]
        y += offset_y
        text = str(rid)
        tw = draw.textlength(text, font=font)
        r = max(11, int(tw / 2) + 8)
        # A region whose visible centre sits against an edge would
        # otherwise get half a disc.  Nudged in rather than dropped: a
        # clipped number is still the only thing identifying that region.
        x = max(fx0 + r + 1, min(fx1 - r - 1, x))
        y = max(fy0 + r + 1, min(fy1 - r - 1, y))
        draw.ellipse(
            [x - r, y - r, x + r, y + r], fill=_CALLOUT_FILL, outline=_INK, width=2
        )
        draw.text((x - tw / 2, y - 8), text, fill=_INK, font=font)


def _draw_legend(
    draw: Any,
    *,
    canvas_w: int,
    canvas_h: int,
    palette: Sequence[tuple[int, int, int]],
    legend_ids: Sequence[int],
    statuses: dict[int, str],
    total_regions: int,
    area_fractions: Sequence[float] | None,
) -> None:
    """Swatch + number + share, so the image names its own regions."""
    x0 = canvas_w - _LEGEND_W
    y0 = _HEADER_H
    y1 = canvas_h - _FOOTER_H
    draw.rectangle([x0, y0, canvas_w, y1], fill=_LEGEND_BG)
    draw.line([(x0, y0), (x0, y1)], fill=_RULE, width=1)

    pad = 16
    draw.text(
        (x0 + pad, y0 + 14), "REGIONS", fill=_FOOTER_FG, font=_font(12, bold=True)
    )
    draw.text(
        (x0 + pad, y0 + 32),
        "label colors, not filament",
        fill=(126, 132, 140),
        font=_font(11),
    )

    row_font = _font(13)
    small_font = _font(11)
    num_font = _font(11, bold=True)
    y = y0 + 58
    row_h = 30
    swatch = 20

    for rid in legend_ids:
        if y + row_h > y1 - 20:
            break
        color = palette[rid] if rid < len(palette) else (160, 160, 160)
        draw.rectangle(
            [x0 + pad, y, x0 + pad + swatch, y + swatch], fill=color, outline=_INK
        )
        num = str(rid)
        nw = draw.textlength(num, font=num_font)
        draw.text(
            (x0 + pad + swatch / 2 - nw / 2, y + 4), num, fill=_INK, font=num_font
        )

        label = f"Region {rid}"
        status = statuses.get(rid, "")
        if status:
            # Say why there is no disc on the object rather than leaving a
            # legend row the reader cannot find anywhere in the picture.
            label += f"  ({status})"
        draw.text((x0 + pad + swatch + 10, y + 2), label, fill=_INK, font=row_font)

        if area_fractions is not None and rid < len(area_fractions):
            share = f"{area_fractions[rid] * 100:.0f}% of surface"
            draw.text(
                (x0 + pad + swatch + 10, y + 17),
                share,
                fill=(120, 126, 134),
                font=small_font,
            )
        y += row_h + (6 if area_fractions is not None else 0)

    remaining = total_regions - len(legend_ids)
    if remaining > 0:
        draw.text(
            (x0 + pad, min(y + 6, y1 - 24)),
            f"+{remaining} smaller region{'s' if remaining != 1 else ''}",
            fill=(126, 132, 140),
            font=small_font,
        )
