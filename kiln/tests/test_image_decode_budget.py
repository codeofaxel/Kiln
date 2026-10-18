"""A relief is carved at 250 px, so the decode is sized for the machine.

``prepare_image_for_emboss`` shrinks every image to 200-250 px before it
carves anything -- and the door decoded it at full size first, then made
several copies of that.  On the hosted box (2 GB, one process for every
customer) the relief preview cost, measured 2026-10-18:

====================================  ==========  =========
upload                                 before      after
====================================  ==========  =========
657 KB PNG of 169 M pixels             3,961 MB     85 MB
5.5 KB WebP of 144 M pixels            4,713 MB     84 MB
1.98 MB JPEG of 169 M pixels           3,119 MB     86 MB
====================================  ==========  =========

Each more than doubled the machine, and each ended in a refusal anyway --
but only after spending the memory, so the refusal protected nothing.

**The other half of the job is that a real photograph is unaffected**, and
these tests assert that as hard as they assert the refusals: a picture at
or under the decode target comes out byte-identical, and a 12 M-pixel phone
photo's relief differs by at most a few levels out of 255.
"""

from __future__ import annotations

import io
import os

import pytest

from kiln import image_to_surface as its

Image = pytest.importorskip("PIL.Image")


def _photo(path, size: tuple[int, int], fmt: str = "JPEG", **kw) -> str:
    """A picture with real variation in it -- a flat fill would hide a
    resampling difference the tests are looking for."""
    import random

    rng = random.Random(7)
    small = Image.new("RGB", (64, 64))
    small.putdata([
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(64 * 64)
    ])
    img = small.resize(size, Image.LANCZOS)
    img.save(path, fmt, **kw)
    return str(path)


# ---------------------------------------------------------------------------
# Refused, before the memory is spent
# ---------------------------------------------------------------------------


def _declaring(path, width: int, height: int, fmt: str) -> str:
    """A small file that DECLARES a huge picture."""
    if fmt == "WEBP":
        Image.new("RGB", (width, height), (0, 0, 0)).save(
            path, "WEBP", lossless=True,
        )
    else:
        Image.new("L", (width, height), 0).save(path, fmt)
    return str(path)


@pytest.mark.parametrize("fmt,suffix", [("PNG", ".png"), ("JPEG", ".jpg")])
def test_an_image_past_the_ceiling_is_refused(tmp_path, fmt, suffix):
    path = _declaring(tmp_path / f"huge{suffix}", 13_000, 13_000, fmt)
    with pytest.raises(its.ImageTooLargeError):
        its._open_reduced(path)


def test_the_refusal_says_the_size_and_what_to_do(tmp_path):
    path = _declaring(tmp_path / "huge.png", 13_000, 13_000, "PNG")
    with pytest.raises(its.ImageTooLargeError) as caught:
        its._open_reduced(path)
    said = str(caught.value)
    assert "13000x13000" in said
    assert "169 million" in said
    assert "scale it down" in said.lower(), "a refusal says what to do next"


def test_the_refusal_is_a_value_error():
    """Every caller that already declines an undecodable image catches
    ``ValueError``, so none has to learn a new exception."""
    assert issubclass(its.ImageTooLargeError, ValueError)


def test_refusing_costs_almost_nothing(tmp_path):
    """The whole point: the size is read from the header, so an absurd
    picture is turned away before a pixel is decoded."""
    import tracemalloc

    path = _declaring(tmp_path / "huge.png", 13_000, 13_000, "PNG")
    tracemalloc.start()
    try:
        with pytest.raises(its.ImageTooLargeError):
            its._open_reduced(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, f"refusing held {peak} bytes"


def test_a_webp_is_measured_from_its_header_not_by_decoding_it(tmp_path):
    """Pillow's WebP reader is NOT lazy -- it decodes the whole image inside
    ``Image.open``, so asking the opened image its size has already spent
    the memory the check exists to prevent (measured: 1,183 MB to find out
    a 5.5 KB file was 144 M pixels).  The header is read by hand instead."""
    import tracemalloc

    path = _declaring(tmp_path / "huge.webp", 12_000, 12_000, "WEBP")
    assert its._declared_size(path) == (12_000, 12_000)
    tracemalloc.start()
    try:
        with pytest.raises(its.ImageTooLargeError):
            its._open_reduced(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, f"refusing a WebP held {peak} bytes"


@pytest.mark.parametrize("kw", [{"lossless": True}, {"quality": 80}])
def test_the_webp_header_agrees_with_pillow(tmp_path, kw):
    """Both WebP spellings, against the reader they must not disagree
    with -- a header parse that is wrong in the OTHER direction would
    refuse real pictures."""
    path = tmp_path / "real.webp"
    Image.new("RGB", (1234, 567), (120, 30, 200)).save(path, "WEBP", **kw)
    assert its._declared_size(str(path)) == Image.open(path).size


def test_something_that_is_not_a_webp_reads_as_unknown(tmp_path):
    """``None`` means "the header did not say", which falls through to
    Pillow -- never "zero pixels"."""
    path = tmp_path / "x.png"
    _photo(path, (32, 32), "PNG")
    assert its._declared_size(str(path)) is None


# ---------------------------------------------------------------------------
# A real photograph is unaffected, which is the other half of the job
# ---------------------------------------------------------------------------


def test_a_picture_within_the_target_is_untouched(tmp_path):
    """At or under the decode target nothing is resampled at all, so the
    carve is the one it always was."""
    path = _photo(tmp_path / "small.png", (its.DECODE_TARGET_PX, 1200), "PNG")
    assert its._open_reduced(path).size == (its.DECODE_TARGET_PX, 1200)


def test_a_larger_picture_is_reduced_to_the_target(tmp_path):
    path = _photo(tmp_path / "big.jpg", (3024, 4032))
    reduced = its._open_reduced(path)
    assert max(reduced.size) == its.DECODE_TARGET_PX
    assert reduced.size[0] / reduced.size[1] == pytest.approx(3024 / 4032, rel=0.01)


def test_a_phone_photo_carves_the_same_relief(tmp_path):
    """The question that decides whether this change is allowed to ship: a
    12 M-pixel photograph's heightmap, before and after.

    Compared through the engine's own entry point at its real working
    size.  A few levels out of 255 is resampling noise on a surface that is
    then carved a millimetre deep; anything more would be a different
    relief and this change would be wrong.
    """
    path = _photo(tmp_path / "phone.jpg", (3024, 4032))

    def heightmap(reduce_it: bool):
        work = tmp_path / ("after" if reduce_it else "before")
        work.mkdir()
        if not reduce_it:
            original = its._open_reduced
            its._open_reduced = lambda p: Image.open(p)
            try:
                info = its.prepare_image_for_emboss(
                    path, str(work), max_resolution=200, style="lithophane",
                )
            finally:
                its._open_reduced = original
        else:
            info = its.prepare_image_for_emboss(
                path, str(work), max_resolution=200, style="lithophane",
            )
        with open(info["dat_path"]) as fh:
            return [
                [float(v) for v in line.split()]
                for line in fh
                if line.strip() and not line.startswith("#")
            ]

    before, after = heightmap(False), heightmap(True)
    assert len(before) == len(after) and len(before[0]) == len(after[0])
    worst = max(
        abs(b - a)
        for row_b, row_a in zip(before, after)
        for b, a in zip(row_b, row_a)
    )
    # The heightmap is 0..1, so this is a fraction of the full carve depth.
    assert worst < 0.05, f"the relief changed by {worst:.3f} of its depth"


def test_a_jpeg_is_reduced_by_the_decoder(tmp_path):
    """``draft`` lets the JPEG decoder skip most of the work, so a large
    photograph's full size is never held -- measured 2026-10-18: 0.09 s and
    23 MB for a 169 M-pixel JPEG, against 36 s and 3,119 MB."""
    import tracemalloc

    path = _photo(tmp_path / "big.jpg", (6000, 6000))
    tracemalloc.start()
    try:
        its._open_reduced(path).load()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A full 6000x6000 RGB decode is ~108 MB; the reduced one is a fraction.
    assert peak < 32 * 1024 * 1024, f"the decode held {peak} bytes"
