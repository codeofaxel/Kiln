"""The shared system-font resolver, and the drift it exists to stop.

Three modules draw text onto a Pillow image, and each used to carry its
own hand-copied list of system font paths.  The lists had already
drifted: different macOS faces first, one caller with a bare ``"Arial"``
nobody else had.  A copied list is a derived value — fix a moved path in
one copy and the others keep quietly painting PIL's built-in face, which
reads as a rendering bug rather than a missing font.

So these are not "does the resolver work" tests.  They are drift pins:
each caller is checked by *emptying the shared list* (or spying on the
shared function) and asserting the caller's own behaviour changes.  A
module that regrows a private list keeps rendering exactly as before,
and that is precisely what fails here.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageFont

from kiln import _fonts, model_visualizer, region_map, stage_paint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_openable(candidates) -> str | None:
    """The first candidate this host can actually open, or ``None``."""
    for cand in candidates:
        try:
            ImageFont.truetype(cand, 12)
        except OSError:
            continue
        return cand
    return None


#: Pillow >= 10.1 answers ``load_default()`` with a real TrueType face;
#: older ones answer with a bitmap font that ``draw.text(anchor=...)``
#: refuses.  Only the former can exercise a caller's degraded path.
_DEFAULT_IS_TRUETYPE = isinstance(ImageFont.load_default(), ImageFont.FreeTypeFont)


@pytest.fixture()
def no_faces(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no installed font the resolver knows about."""
    monkeypatch.setattr(_fonts, "_CANDIDATES_REGULAR", ())
    monkeypatch.setattr(_fonts, "_CANDIDATES_BOLD", ())


@pytest.fixture()
def fake_renders(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """``compare_renders`` with its OpenSCAD leg replaced by real PNGs.

    The label-drawing code sits past every render, so reaching it at all
    means standing in for :func:`visualize_model`.
    """
    w, h = 40, 30
    sources = []
    for name in ("a", "b"):
        model = tmp_path / f"{name}.stl"
        model.write_bytes(b"")  # only ``os.path.isfile`` looks at it
        png = tmp_path / f"{name}.png"
        Image.new("RGB", (w, h), (10, 20, 30)).save(png, "PNG")
        sources.append((str(model), str(png)))

    renders = dict(sources)

    def fake_visualize_model(path, **kwargs):
        return {"success": True, "views": [{"path": renders[path]}]}

    monkeypatch.setattr(model_visualizer, "visualize_model", fake_visualize_model)

    def run():
        return model_visualizer.compare_renders(
            [p for p, _ in sources],
            width=w,
            height=h,
            output_path=str(tmp_path / "compare.png"),
        )

    return run


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bold", [False, True])
def test_find_font_skips_what_will_not_open_and_honours_order(
    bold: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real face at the asked size, taken from the shared list in order."""
    which = "_CANDIDATES_BOLD" if bold else "_CANDIDATES_REGULAR"
    real = _first_openable(getattr(_fonts, which))
    if real is None:
        pytest.skip("host has no font from the shared candidate list")

    monkeypatch.setattr(_fonts, which, ("/nonexistent/not-a-font.ttf", real))
    font = _fonts.find_font(19, bold=bold)

    assert font is not None
    assert font.path == real
    assert font.size == 19


def test_find_font_says_none_when_the_host_has_nothing(no_faces) -> None:
    """No real face means ``None`` — for callers that can draw nothing."""
    assert _fonts.find_font(19) is None
    assert _fonts.find_font(19, bold=True) is None


def test_load_font_degrades_to_pils_own_face(no_faces) -> None:
    """No real face means PIL's built-in one — for callers that must draw."""
    default = ImageFont.load_default()

    for bold in (False, True):
        got = _fonts.load_font(19, bold=bold)
        assert type(got) is type(default)
        # The built-in face ignores the asked size and has no file behind
        # it; either would be wrong for a face off the candidate list.
        assert getattr(got, "size", None) == getattr(default, "size", None)
        assert not isinstance(getattr(got, "path", None), str)


def test_load_font_prefers_a_real_face_over_the_fallback() -> None:
    if _first_openable(_fonts._CANDIDATES_REGULAR) is None:
        pytest.skip("host has no font from the shared candidate list")

    font = _fonts.load_font(19)
    assert font.path in _fonts._CANDIDATES_REGULAR
    assert font.size == 19


# ---------------------------------------------------------------------------
# Caller: stage_paint's plate stamp
# ---------------------------------------------------------------------------


def test_plate_stamp_reads_the_shared_bold_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emptying the shared list must take stage_paint's stamp with it.

    A private list in stage_paint would paint the identical plate both
    times, and this fails.
    """
    if _first_openable(_fonts._CANDIDATES_BOLD) is None:
        pytest.skip("host has no bold face; the stamp is skipped either way")

    stamped = np.asarray(stage_paint._plate_texture(None))
    monkeypatch.setattr(_fonts, "_CANDIDATES_BOLD", ())
    bare = np.asarray(stage_paint._plate_texture(None))

    differs = np.any(stamped != bare, axis=-1)
    assert differs.any(), (
        "emptying the shared bold list left the plate byte-identical — "
        "stage_paint is not resolving through kiln._fonts"
    )

    # ...and what it took was the corner stamp, nothing else on the plate.
    rows, cols = np.nonzero(differs)
    height, width = differs.shape
    assert rows.min() > height * 0.8
    assert cols.min() > width * 0.8


def test_plate_stamp_is_skipped_rather_than_drawn_in_the_fallback_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp is sized off the texture, so PIL's fixed-size face would
    smudge the corner.  stage_paint asks ``find_font`` for exactly that
    reason, and gets to draw nothing."""
    if _first_openable(_fonts._CANDIDATES_BOLD) is None:
        pytest.skip("host has no bold face; the stamp is skipped either way")

    # The stamp is warm ink (#FF6B2B at 18%) on a cool blue-grey plate, so
    # "red above blue" finds it without pinning the composited value.
    def warm_corner_pixels() -> int:
        plate = np.asarray(stage_paint._plate_texture(None)).astype(int)
        corner = plate[int(plate.shape[0] * 0.8) :, int(plate.shape[1] * 0.8) :]
        return int((corner[..., 0] > corner[..., 2]).sum())

    assert warm_corner_pixels() > 0  # control: the stamp is there to lose

    monkeypatch.setattr(_fonts, "_CANDIDATES_BOLD", ())
    assert warm_corner_pixels() == 0


# ---------------------------------------------------------------------------
# Caller: model_visualizer's comparison labels
# ---------------------------------------------------------------------------


def test_comparison_labels_resolve_through_the_shared_door(
    fake_renders, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels must come from ``kiln._fonts``, not a list of their own."""
    asked: list[tuple[int, bool]] = []
    real_load_font = _fonts.load_font

    def spy(size: int, *, bold: bool = False):
        asked.append((size, bold))
        return real_load_font(size, bold=bold)

    monkeypatch.setattr(_fonts, "load_font", spy)
    result = fake_renders()

    assert result["success"], result.get("error")
    assert asked == [(16, False)], (
        "compare_renders drew its labels without asking kiln._fonts"
    )


@pytest.mark.skipif(
    not _DEFAULT_IS_TRUETYPE,
    reason="Pillow's built-in face is a bitmap font here, which compare_renders' "
    "anchored labels cannot draw with — a gap that predates the shared resolver",
)
def test_comparison_labels_still_draw_with_no_face(fake_renders, no_faces) -> None:
    """Labels degrade to PIL's own face rather than vanishing."""
    result = fake_renders()

    assert result["success"], result.get("error")
    with Image.open(result["comparison_path"]) as img:
        assert img.size[0] > 0 and img.size[1] > 0


# ---------------------------------------------------------------------------
# Caller: region_map's header, legend and callouts
# ---------------------------------------------------------------------------


def _two_region_square():
    """The smallest input the map renderer accepts: two faces, two regions."""
    tris = [
        ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (20.0, 20.0, 0.0)),
        ((0.0, 0.0, 0.0), (20.0, 20.0, 0.0), (0.0, 20.0, 0.0)),
    ]
    return tris, [0, 1]


def test_region_map_chrome_reads_the_shared_list(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Emptying the shared list must change the map's burned-in chrome.

    The header strip is where the "these are labels, not filament"
    sentence lives, so it is drawn text and nothing else.  A private list
    inside region_map would render it identically both times.
    """
    if _first_openable(_fonts._CANDIDATES_REGULAR) is None:
        pytest.skip("host has no font from the shared candidate list")

    tris, face_region = _two_region_square()

    def header_band(name: str) -> np.ndarray:
        out = tmp_path / name
        region_map.render_region_map(
            tris,
            face_region,
            output_path=str(out),
            width=500,
            height=380,
            supersample=1,
        )
        with Image.open(out) as img:
            return np.asarray(img.convert("RGB"))[: region_map._HEADER_H]

    lettered = header_band("with-faces.png")
    monkeypatch.setattr(_fonts, "_CANDIDATES_REGULAR", ())
    monkeypatch.setattr(_fonts, "_CANDIDATES_BOLD", ())
    fallback = header_band("no-faces.png")

    assert not np.array_equal(lettered, fallback), (
        "emptying the shared list left the region-map header byte-identical — "
        "region_map is not resolving through kiln._fonts"
    )


def test_region_map_still_renders_with_no_face(tmp_path, no_faces) -> None:
    """The disclaimer must still get burned in, in PIL's own face."""
    tris, face_region = _two_region_square()
    out = tmp_path / "no-faces.png"

    result = region_map.render_region_map(
        tris, face_region, output_path=str(out), width=500, height=380, supersample=1
    )

    assert result.path == str(out)
    assert out.stat().st_size > 0
