"""An image decoration must never carve its own rectangle into the part.

A heightmap drives a subtractive ``surface()``: value v carves v x depth.
So a heightmap whose OUTER BORDER is non-zero cuts a trench right around
the artwork, and the printed part wears a sunken photo frame — the source
image's rectangle, embossed into the object. Nobody ever wants that, and
it is equally wrong for emboss and deboss.

It shipped because the shape mask lived inside the coin pipeline, and that
pipeline aborted wholesale when the optional ``rembg`` package was absent.
The fallback path did no masking, so on any machine without rembg every
photo decoration carried the frame (found on a real coaster, 2026-07-29).

The invariant here is deliberately style-agnostic and dependency-agnostic:
whatever the style, and whatever optional dependency is missing, the
heightmap's border must be at the no-carve level.
"""

from __future__ import annotations

import pathlib

import pytest

from kiln.image_to_surface import prepare_image_for_emboss

pytest.importorskip("PIL")


def _mark_on_white(path: pathlib.Path, size=(400, 280)) -> pathlib.Path:
    """A logo-like mark: dark artwork on a clean white field."""
    from PIL import Image, ImageDraw

    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    d.polygon(
        [(140, 200), (170, 90), (230, 90), (260, 200)], outline=0, width=8
    )
    d.rectangle([150, 150, 250, 160], fill=0)
    img.save(path)
    return path


def _photo_like(path: pathlib.Path, size=(400, 280)) -> pathlib.Path:
    """Continuous-tone content that reaches every edge."""
    from PIL import Image

    img = Image.new("L", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (x * 3 + y * 5) % 256
    img.save(path)
    return path


def _border_values(dat_path: str) -> list[float]:
    rows = [
        [float(v) for v in line.split()]
        for line in pathlib.Path(dat_path).read_text().splitlines()
        if line.strip()
    ]
    top, bottom = rows[0], rows[-1]
    left = [r[0] for r in rows]
    right = [r[-1] for r in rows]
    return top + bottom + left + right


@pytest.mark.parametrize("style", ["coin", "photo", "stencil", "default"])
@pytest.mark.parametrize("mask", ["auto", "circle", "rectangle", "rounded_rectangle"])
def test_emboss_never_carves_the_image_rectangle(tmp_path, style, mask):
    """No style, no mask choice, may leave the border carving."""
    src = _mark_on_white(tmp_path / "mark.png")
    out = tmp_path / f"out_{style}_{mask}"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style=style, mask=mask
    )
    border = _border_values(hm["dat_path"])

    assert border, "heightmap had no rows"
    worst = max(border)
    assert worst == pytest.approx(0.0, abs=1e-6), (
        f"style={style!r} mask={mask!r}: the heightmap border carves "
        f"(max {worst:.3f} of full depth), so the source image's rectangle "
        "is cut into the part as a sunken frame"
    )


def test_photo_content_still_carves_inside_the_shape(tmp_path):
    """The guarantee must not flatten the artwork itself."""
    src = _photo_like(tmp_path / "photo.png")
    out = tmp_path / "out"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style="coin", mask="circle"
    )
    rows = [
        [float(v) for v in line.split()]
        for line in pathlib.Path(hm["dat_path"]).read_text().splitlines()
        if line.strip()
    ]
    interior = [v for r in rows[2:-2] for v in r[2:-2]]
    assert max(interior) > 0.15, (
        "the interior lost all relief — the mask is eating the artwork, "
        "not just the boundary"
    )


def test_missing_background_removal_still_masks(tmp_path, monkeypatch):
    """rembg is an enhancement; without it the mask must still apply.

    Replays the actual failure: the coin pipeline aborted when rembg was
    absent and the fallback carved the full rectangle.
    """
    import builtins

    real_import = builtins.__import__

    def _no_rembg(name, *args, **kwargs):
        if name == "rembg" or name.startswith("rembg."):
            raise ImportError("rembg unavailable (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_rembg)

    src = _mark_on_white(tmp_path / "mark.png")
    out = tmp_path / "out"
    out.mkdir()
    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style="coin", mask="auto"
    )
    assert max(_border_values(hm["dat_path"])) == pytest.approx(0.0, abs=1e-6), (
        "without rembg the coin path skipped its mask and carved the "
        "image rectangle into the part"
    )


def test_a_mark_carves_only_its_own_ink(tmp_path):
    """A logo must not sit in a pool: only the artwork displaces the surface.

    The carve fraction must track the artwork's ink coverage, not the area
    of any mask shape. A pool (circle or otherwise) around a mark carves
    the field, and the field of a logo is empty space that belongs to the
    part's own surface.
    """
    from PIL import Image, ImageDraw

    size = (400, 300)
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    d.rectangle([120, 90, 280, 120], fill=0)
    d.rectangle([170, 120, 230, 220], fill=0)
    ink = sum(1 for v in img.getdata() if v < 128) / (size[0] * size[1])
    src = tmp_path / "mark.png"
    img.save(src)
    out = tmp_path / "out"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style="coin", mask="auto"
    )
    rows = [
        [float(v) for v in line.split()]
        for line in pathlib.Path(hm["dat_path"]).read_text().splitlines()
        if line.strip()
    ]
    vals = [v for r in rows for v in r]
    carve = sum(1 for v in vals if v > 0.3) / len(vals)
    assert carve == pytest.approx(ink, abs=0.05), (
        f"carve fraction {carve:.3f} vs ink coverage {ink:.3f} — the engine "
        "is carving a pool around the mark, not just the mark"
    )
