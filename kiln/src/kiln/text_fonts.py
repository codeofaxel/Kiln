"""The fonts Kiln carves words with.

One list, read by every door that puts text on a model.  Each font has a
short id a caller passes (``font="mono"``), a label a person reads, and the
face the text engine draws.  Every font here is free on every plan.

Two of them are on every machine the text engine runs on; the rest ship with
Kiln in ``data/fonts``, each beside its own license, and every text engine
launch is pointed at that folder.

The engine draws its default face in place of a font it cannot find, and
says nothing, so a font that is not really there would carve the wrong
letters and report success.  Each font therefore records how much it draws,
and :func:`fonts_not_drawn` measures every one against that record.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_TEXT_FONT",
    "TEXT_FONTS",
    "TextFont",
    "UnknownTextFontError",
    "drawn_ink",
    "fonts_not_drawn",
    "list_text_fonts",
    "resolve_text_font",
]

#: The words and size every font's ink is measured on.
_INK_TEXT = "Kiln 12"
_INK_SIZE_MM = 10.0
#: How far a font's measured ink and width may sit from the recorded ones and
#: still be that font at that weight.  Every other font on the list, and the
#: nearest other weight of each, is further away than this on one of the two.
_INK_TOLERANCE = 0.03


@dataclass(frozen=True)
class TextFont:
    """One font a word can be carved in."""

    #: What a caller passes and what a saved word records.
    id: str
    #: What a person reads in a list of fonts.
    label: str
    #: One line on what the font suits.
    note: str
    #: The face the text engine draws.
    spec: str
    #: Area, in square mm, and width, in mm, of :data:`_INK_TEXT` drawn in
    #: this font at :data:`_INK_SIZE_MM`.  A font the engine cannot find, or
    #: finds at another weight, draws a different area or width.
    ink_mm2: float
    ink_width_mm: float

    def to_dict(self) -> dict[str, str]:
        """The font as a caller sees it: no engine detail."""
        return {"id": self.id, "label": self.label, "note": self.note}


#: The font a word is carved in when the caller names none.
DEFAULT_TEXT_FONT = "sans"

TEXT_FONTS: tuple[TextFont, ...] = (
    TextFont(
        id="sans",
        label="Sans",
        note="Plain bold letters. Reads well at any size; the usual choice.",
        spec="Liberation Sans:style=Bold",
        ink_mm2=172.2,
        ink_width_mm=44.1,
    ),
    TextFont(
        id="mono",
        label="Mono",
        note="Even-width bold letters. Suits labels, part numbers and codes.",
        spec="Liberation Mono:style=Bold",
        ink_mm2=186.5,
        ink_width_mm=56.6,
    ),
    TextFont(
        id="fredoka",
        label="Fredoka",
        note="Rounded, friendly bold letters. Suits names, gifts and kids' things.",
        spec="Fredoka:style=Bold",
        ink_mm2=220.5,
        ink_width_mm=39.3,
    ),
    TextFont(
        id="zilla-slab",
        label="Zilla Slab",
        note="Sturdy letters with thick square feet. Suits plaques, signs and tools.",
        spec="Zilla Slab:style=Bold",
        ink_mm2=167.2,
        ink_width_mm=40.5,
    ),
    TextFont(
        id="oswald",
        label="Oswald",
        note="Tall, narrow bold letters. Fits a long word on a small face.",
        spec="Oswald:style=Bold",
        ink_mm2=211.8,
        ink_width_mm=37.2,
    ),
    TextFont(
        id="saira-stencil",
        label="Saira Stencil",
        note="Stencil letters with bridges, so a cut-through word keeps its centres.",
        spec="Saira Stencil One:style=Regular",
        ink_mm2=218.1,
        ink_width_mm=42.1,
    ),
)

_BY_ID = {font.id: font for font in TEXT_FONTS}


class UnknownTextFontError(ValueError):
    """A caller named a font that is not on the list."""

    def __init__(self, asked: str) -> None:
        self.asked = asked
        self.choices = [font.id for font in TEXT_FONTS]
        super().__init__(
            f"Unknown font {asked!r}. Choose one of: {', '.join(self.choices)}."
        )


def resolve_text_font(font: str | None = None) -> TextFont:
    """The font for *font*, or the default when none is named.

    :raises UnknownTextFontError: *font* is not an id on the list.
    """
    wanted = (font or "").strip().lower()
    if not wanted:
        return _BY_ID[DEFAULT_TEXT_FONT]
    found = _BY_ID.get(wanted)
    if found is None:
        raise UnknownTextFontError(font or "")
    return found


def list_text_fonts() -> list[dict[str, str]]:
    """Every font a word can be carved in, default first."""
    ordered = sorted(TEXT_FONTS, key=lambda f: f.id != DEFAULT_TEXT_FONT)
    return [font.to_dict() for font in ordered]


def drawn_ink(spec: str) -> tuple[float, float]:
    """``(area_mm2, width_mm)`` the text engine really draws for *spec*.

    :raises RuntimeError: the text engine is missing or the draw failed.
    """
    import os
    import tempfile

    import trimesh

    from kiln.emboss_generator import _find_openscad
    from kiln.openscad_runner import run_openscad

    with tempfile.TemporaryDirectory(prefix="kiln_font_ink_") as work:
        scad = os.path.join(work, "ink.scad")
        stl = os.path.join(work, "ink.stl")
        face = spec.replace("\\", "\\\\").replace('"', '\\"')
        with open(scad, "w", encoding="utf-8") as fh:
            fh.write(
                f'linear_extrude(1) text("{_INK_TEXT}", size={_INK_SIZE_MM}, font="{face}");\n'
            )
        try:
            done = run_openscad([_find_openscad(), "-o", stl, scad], timeout=60, output_path=stl)
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(f"the text engine did not run: {exc}") from exc
        if done.returncode != 0 or not os.path.exists(stl):
            raise RuntimeError(f"the text engine could not draw {spec!r}")
        drawn = trimesh.load(stl)
        # Extruded 1 mm, so the volume is the area.
        return float(drawn.volume), float(drawn.extents[0])


def fonts_not_drawn() -> list[str]:
    """Ids of the fonts this machine does not draw as recorded; ``[]`` is all good.

    Run it where Kiln is installed to carve for other people: a font named
    here would be carved as some other face with no error.
    """
    wrong = []
    for font in TEXT_FONTS:
        ink, width = drawn_ink(font.spec)
        if (
            abs(ink - font.ink_mm2) > _INK_TOLERANCE * font.ink_mm2
            or abs(width - font.ink_width_mm) > _INK_TOLERANCE * font.ink_width_mm
        ):
            wrong.append(font.id)
    return wrong
