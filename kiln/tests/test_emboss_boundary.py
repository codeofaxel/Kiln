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

It is also invert-agnostic, and that axis is load-bearing: the production
deboss door passes ``invert=True`` (see the decorate_surface image branch),
and this suite originally ran only the default ``invert=False`` — so the
border invariant was green at a parameter value the real door never uses,
while every logo AND photo deboss shipped the frame (caught on a render,
2026-08-18).  Two orderings caused it: ``_invert`` ran after
``_flatten_field`` (whose output is polarity-free), and after ``_mask_rows``
(whose zeros are geometry, not luminance).  Both flips turned "flush" into
"full depth".  Transparency is the third axis: ``convert("L")`` drops the
alpha channel, decoding a transparent surround as black, so the fixtures
below cover opaque and transparent sources alike.
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


def _mark_on_transparent(path: pathlib.Path, size=(400, 280)) -> pathlib.Path:
    """The same logo shape a brand actually ships: opaque ink, alpha surround."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon(
        [(140, 200), (170, 90), (230, 90), (260, 200)],
        outline=(0, 0, 0, 255), width=8,
    )
    d.rectangle([150, 150, 250, 160], fill=(0, 0, 0, 255))
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


@pytest.mark.parametrize("invert", [False, True], ids=["emboss", "deboss"])
@pytest.mark.parametrize("source", ["opaque", "transparent"])
@pytest.mark.parametrize("style", ["coin", "photo", "stencil", "default"])
@pytest.mark.parametrize("mask", ["auto", "circle", "rectangle", "rounded_rectangle"])
def test_emboss_never_carves_the_image_rectangle(tmp_path, style, mask, source, invert):
    """No style, no mask choice, no polarity, no alpha may leave the border carving."""
    if source == "opaque":
        src = _mark_on_white(tmp_path / "mark.png")
    else:
        src = _mark_on_transparent(tmp_path / "mark_alpha.png")
    out = tmp_path / f"out_{style}_{mask}_{source}_{invert}"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style=style, mask=mask,
        invert=invert,
    )
    border = _border_values(hm["dat_path"])

    assert border, "heightmap had no rows"
    worst = max(border)
    assert worst == pytest.approx(0.0, abs=1e-6), (
        f"style={style!r} mask={mask!r} source={source!r} invert={invert!r}: "
        f"the heightmap border carves (max {worst:.3f} of full depth), so "
        "the source image's rectangle is cut into the part as a sunken frame"
    )


@pytest.mark.parametrize("invert", [False, True], ids=["emboss", "deboss"])
def test_photo_content_still_carves_inside_the_shape(tmp_path, invert):
    """The guarantee must not flatten the artwork itself — in either polarity."""
    src = _photo_like(tmp_path / "photo.png")
    out = tmp_path / f"out_{invert}"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style="coin", mask="circle",
        invert=invert,
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


@pytest.mark.parametrize("invert", [False, True], ids=["emboss", "deboss"])
@pytest.mark.parametrize("source", ["opaque", "transparent"])
def test_a_mark_carves_only_its_own_ink(tmp_path, source, invert):
    """A logo must not sit in a pool: only the artwork displaces the surface.

    The carve fraction must track the artwork's ink coverage, not the area
    of any mask shape. A pool (circle or otherwise) around a mark carves
    the field, and the field of a logo is empty space that belongs to the
    part's own surface.

    The deboss cases replay the shipped defect directly: ``invert=True`` is
    what decorate_surface passes for ``mode="deboss"``, and before the fix
    the flip ran after ``_flatten_field``, so the FIELD carved at full depth
    and the mark stood untouched — a sunken box around the logo (~94% carve
    fraction against ~4% ink).  The transparent cases pin the alpha half:
    a dropped alpha channel decodes the surround as ink and carves the
    mark's whole bounding box.
    """
    from PIL import Image, ImageDraw

    size = (400, 300)
    if source == "opaque":
        img = Image.new("L", size, 255)
        d = ImageDraw.Draw(img)
        d.rectangle([120, 90, 280, 120], fill=0)
        d.rectangle([170, 120, 230, 220], fill=0)
        ink = sum(1 for v in img.getdata() if v < 128) / (size[0] * size[1])
    else:
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([120, 90, 280, 120], fill=(0, 0, 0, 255))
        d.rectangle([170, 120, 230, 220], fill=(0, 0, 0, 255))
        ink = sum(1 for p in img.getdata() if p[3] > 128) / (size[0] * size[1])
    src = tmp_path / "mark.png"
    img.save(src)
    out = tmp_path / "out"
    out.mkdir()

    hm = prepare_image_for_emboss(
        str(src), str(out), max_resolution=120, style="coin", mask="auto",
        invert=invert,
    )
    rows = [
        [float(v) for v in line.split()]
        for line in pathlib.Path(hm["dat_path"]).read_text().splitlines()
        if line.strip()
    ]
    vals = [v for r in rows for v in r]
    carve = sum(1 for v in vals if v > 0.3) / len(vals)
    assert carve == pytest.approx(ink, abs=0.05), (
        f"source={source!r} invert={invert!r}: carve fraction {carve:.3f} vs "
        f"ink coverage {ink:.3f} — the engine is carving a pool around the "
        "mark, not just the mark"
    )


def test_transparent_and_flattened_sources_produce_identical_heightmaps(tmp_path):
    """Alpha must mean "empty field", byte-for-byte.

    A transparent-surround image and the same image pre-flattened onto
    white must produce the SAME .dat through a style path.  This is the
    contract that makes every style block safe without reasoning about
    each one's filter chain: if ``convert("L")`` ever drops alpha again,
    the surround decodes as black and the outputs diverge immediately.
    """
    import random

    from PIL import Image

    random.seed(7)
    size = (200, 200)
    src_rgba = Image.new("RGBA", size, (0, 0, 0, 0))
    px = src_rgba.load()
    # Noisy interior so the mark detector reads "photo" and the style
    # preprocessing path (the historical alpha-dropping door) actually runs.
    for y in range(20, 180):
        for x in range(20, 180):
            g = random.randrange(256)
            px[x, y] = (g, g, g, 255)
    p_rgba = tmp_path / "noisy.png"
    src_rgba.save(p_rgba)

    flat = Image.new("RGBA", size, (255, 255, 255, 255))
    flat.paste(src_rgba, mask=src_rgba.split()[3])
    p_flat = tmp_path / "noisy_flat.png"
    flat.convert("RGB").save(p_flat)

    out_a = tmp_path / "out_a"
    out_a.mkdir()
    out_b = tmp_path / "out_b"
    out_b.mkdir()
    hm_a = prepare_image_for_emboss(
        str(p_rgba), str(out_a), invert=True, style="photo", mask="none"
    )
    hm_b = prepare_image_for_emboss(
        str(p_flat), str(out_b), invert=True, style="photo", mask="none"
    )
    dat_a = pathlib.Path(hm_a["dat_path"]).read_text()
    dat_b = pathlib.Path(hm_b["dat_path"]).read_text()
    assert dat_a == dat_b, (
        "a transparent surround and a white surround diverged — an "
        "alpha-dropping convert is back in the pipeline"
    )
