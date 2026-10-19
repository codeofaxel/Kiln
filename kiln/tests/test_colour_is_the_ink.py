"""A logo's colours are brand colours, not depths -- so the colour is the ink.

The alpha rule already says it for transparent artwork: a white stroke and
an orange stroke are the same mark and want the same cut, and reading
brightness there is exactly wrong.  This is the same rule for a logo with
no alpha channel.  Read by brightness alone, a red mark on a green field of
the same brightness vanished -- measured 2026-10-18 on a real coaster
through ``decorate_surface``: red 76, green 73, of 255.  In one style the
engine refused the file as blank ("no visible content to carve"); in
another it carved noise.  Neither told the person what had happened.

Now the colours say where the mark is.  These tests pin:

* the red-on-green logo carves EXACTLY as its black-on-white twin;
* a photograph is untouched -- including a shaded product on a white
  field, which is the shape most likely to be mistaken for a logo;
* two shades of one colour are not told apart, and that bound is stated;
* the result says in words what it read;
* and the A/B: with the colour rule removed, the old refusal returns.
"""

from __future__ import annotations

import random

import pytest

from kiln import image_to_surface as its

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

RED = (255, 0, 0)
GREEN = (0, 124, 0)   # luminance 73 against red's 76: invisible to a brightness read
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def _logo(path, ground, mark, size: int = 400) -> str:
    """A ring, a cross cut through it, and a diamond -- the shapes used in
    the measured coaster."""
    img = Image.new("RGB", (size, size), ground)
    draw = ImageDraw.Draw(img)
    s = size / 800
    draw.ellipse((150 * s, 150 * s, 650 * s, 650 * s), fill=mark)
    draw.rectangle((330 * s, 120 * s, 470 * s, 680 * s), fill=ground)
    draw.rectangle((120 * s, 330 * s, 680 * s, 470 * s), fill=ground)
    draw.polygon([(400 * s, 230 * s), (470 * s, 400 * s), (400 * s, 570 * s), (330 * s, 400 * s)], fill=mark)
    img.save(path)
    return str(path)


def _heightmap(path: str, work, style: str = "stencil"):
    info = its.prepare_image_for_emboss(path, str(work), max_resolution=120, style=style)
    with open(info["dat_path"]) as fh:
        rows = [[float(v) for v in line.split()] for line in fh if line.strip() and not line.startswith("#")]
    return info, rows


# ---------------------------------------------------------------------------
# The logo that vanished
# ---------------------------------------------------------------------------


def test_red_on_green_carves_exactly_as_black_on_white(tmp_path):
    """The whole point, as one assertion: colour-blind to WHICH colours."""
    red_green = _logo(tmp_path / "rg.png", GREEN, RED)
    black_white = _logo(tmp_path / "bw.png", WHITE, BLACK)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    info_rg, rows_rg = _heightmap(red_green, tmp_path / "a")
    info_bw, rows_bw = _heightmap(black_white, tmp_path / "b")
    assert info_rg["treatment"] == "mark" == info_bw["treatment"]
    assert len(rows_rg) == len(rows_bw) and len(rows_rg[0]) == len(rows_bw[0])
    differing = sum(
        1 for ra, rb in zip(rows_rg, rows_bw) for a, b in zip(ra, rb) if abs(a - b) > 0.01
    )
    total = len(rows_rg) * len(rows_rg[0])
    assert differing / total < 0.01, f"{differing} of {total} cells differ"


def test_the_mark_is_the_figure_not_the_ground(tmp_path):
    """The red shapes are carved; the green field stays flush.  Checked at
    the centre of the diamond (mark) and in a corner (ground)."""
    path = _logo(tmp_path / "rg.png", GREEN, RED)
    (tmp_path / "w").mkdir()
    _info, rows = _heightmap(path, tmp_path / "w")
    h, w = len(rows), len(rows[0])
    centre, corner = rows[h // 2][w // 2], rows[2][2]
    assert centre != corner
    assert corner == 0.0, "the field must be flush with the part"


@pytest.mark.parametrize("style", ["lithophane", "stencil", "coin", "auto", "default"])
def test_every_style_reads_the_coloured_logo_as_a_mark(tmp_path, style):
    """Decided on the source before any style, so no style can lose it."""
    path = _logo(tmp_path / "rg.png", GREEN, RED)
    (tmp_path / "w").mkdir()
    info, _rows = _heightmap(path, tmp_path / "w", style=style)
    assert info["treatment"] == "mark"


def test_the_result_says_what_it_read(tmp_path):
    path = _logo(tmp_path / "rg.png", GREEN, RED)
    (tmp_path / "w").mkdir()
    info, _rows = _heightmap(path, tmp_path / "w")
    said = info["read_as"]
    assert said and "2-colour mark" in said and "background" in said


def test_a_logo_that_is_mostly_mark_keeps_the_border_colour_as_ground(tmp_path):
    """The inverse case the panel asked for: a big red disc on a thin
    green border.  Area would pick red as the ground; border contact
    picks green, which is right."""
    img = Image.new("RGB", (300, 300), GREEN)
    ImageDraw.Draw(img).ellipse((10, 10, 290, 290), fill=RED)
    path = tmp_path / "disc.png"
    img.save(path)
    decided = its._colour_decision(img)
    assert decided is not None
    ink = decided[0]
    assert ink.getpixel((150, 150)) == 0, "the disc is the mark"
    assert ink.getpixel((2, 2)) == 255, "the border colour is the ground"


# ---------------------------------------------------------------------------
# What must NOT be read as a logo
# ---------------------------------------------------------------------------


def test_a_photograph_is_left_to_the_brightness_path():
    rng = random.Random(3)
    small = Image.new("RGB", (48, 48))
    small.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(48 * 48)])
    photo = small.resize((400, 400), Image.LANCZOS)
    assert its._colour_decision(photo) is None


def test_a_shaded_product_on_white_is_not_a_logo():
    """The photograph most likely to be mistaken for artwork: one object on
    a clean white field, but SHADED -- its tones are the relief."""
    img = Image.new("RGB", (400, 400), WHITE)
    px = img.load()
    cx, cy, r = 200, 200, 150
    for y in range(400):
        for x in range(400):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if d2 <= r * r:
                shade = int(255 * (1 - (d2 / (r * r)) ** 0.5) * 0.9)
                px[x, y] = (shade, int(shade * 0.3), int(shade * 0.2))
    assert its._colour_decision(img) is None


def test_two_shades_of_one_colour_are_not_told_apart(tmp_path):
    """Stated bound: a mark only slightly lighter than its ground is not a
    colour difference a carve could honour either."""
    img = Image.new("RGB", (300, 300), (0, 124, 0))
    ImageDraw.Draw(img).ellipse((60, 60, 240, 240), fill=(0, 150, 0))
    assert its._colour_decision(img) is None


def test_a_truly_blank_picture_is_still_refused(tmp_path):
    """The refusal is kept for what it was written for: one tone end to
    end, in colour AND brightness."""
    path = tmp_path / "blank.png"
    Image.new("RGB", (200, 200), GREEN).save(path)
    (tmp_path / "w").mkdir()
    with pytest.raises(ValueError, match="no visible content"):
        its.prepare_image_for_emboss(str(path), str(tmp_path / "w"), max_resolution=100)


# ---------------------------------------------------------------------------
# The A/B
# ---------------------------------------------------------------------------


def test_without_the_colour_rule_the_old_refusal_returns(tmp_path, monkeypatch):
    """Mutation pin: take the rule away and the visible logo is called
    blank again, which is the measured 2026-10-18 behaviour."""
    path = _logo(tmp_path / "rg.png", GREEN, RED)
    (tmp_path / "w").mkdir()
    monkeypatch.setattr(its, "_colour_decision", lambda img: None)
    with pytest.raises(ValueError, match="no visible content"):
        its.prepare_image_for_emboss(path, str(tmp_path / "w"), max_resolution=100, style="lithophane")


# ---------------------------------------------------------------------------
# The door the tool actually uses
# ---------------------------------------------------------------------------
#
# decorate_surface does not carve a logo through the heightmap at all: it
# traces the ink into polygons (prepare_logo_image_for_emboss ->
# mark_geometry.trace_image_to_mark), and that tracer opened the image
# itself.  Fixing the heightmap door alone left the tool carving noise
# (measured through decorate_surface on a real coaster, 2026-10-18) -- the
# one-door fallacy, one layer down.  These tests go through the tracer.


def _traced_rings(path: str):
    from kiln.mark_geometry import trace_image_to_mark

    mark = trace_image_to_mark(path, max_dim=256)
    assert mark is not None and not mark.is_empty
    return mark


def test_the_tracer_reads_red_on_green_as_it_reads_black_on_white(tmp_path):
    rg = _traced_rings(_logo(tmp_path / "rg.png", GREEN, RED))
    bw = _traced_rings(_logo(tmp_path / "bw.png", WHITE, BLACK))
    # Same shapes traced: the same ring groups, the same size, the same
    # content bounds.
    assert len(rg.groups) == len(bw.groups)
    assert sum(len(g) for g in rg.groups) == sum(len(g) for g in bw.groups)
    assert (rg.width, rg.height) == (bw.width, bw.height)
    assert rg.content_bounds_info() == bw.content_bounds_info()


def test_the_tool_door_reads_the_coloured_logo_as_a_mark(tmp_path):
    """prepare_logo_image_for_emboss: the function decorate_surface
    imports.  Before the fix it raised "No traceable mark found", or traced
    noise."""
    path = _logo(tmp_path / "rg.png", GREEN, RED)
    (tmp_path / "w").mkdir()
    info = its.prepare_logo_image_for_emboss(path, str(tmp_path / "w"))
    assert info.get("openscad_polygons")
    assert info["width"] > 0 and info["height"] > 0


def test_auto_style_routes_the_coloured_logo_to_the_mark_path(tmp_path):
    """``image_style="auto"`` asks is_bilevel_image, which reads through the
    same loader -- so a coloured logo is now bilevel and goes where a logo
    goes."""
    from kiln.mark_geometry import is_bilevel_image

    assert is_bilevel_image(_logo(tmp_path / "rg.png", GREEN, RED))
    assert is_bilevel_image(_logo(tmp_path / "bw.png", WHITE, BLACK))


def test_the_tracer_no_longer_opens_the_image_itself():
    """Pinned structurally: a second ``Image.open`` is a second door,
    however much of the rest is shared -- that is how this one drifted."""
    import inspect

    from kiln import mark_geometry

    source = inspect.getsource(mark_geometry._load_flattened_grayscale)
    assert "Image.open(" not in source
    assert "_open_grayscale" in source
