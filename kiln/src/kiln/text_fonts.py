"""The fonts Kiln carves words with.

One list, read by every door that puts text on a model.  Each font has a
short id a caller passes (``font="mono"``), a label a person reads, and the
face the text engine draws.  Every font here is free on every plan.

A font joins this list only when the text engine can draw it wherever Kiln
runs.  The engine draws its default face in place of one it cannot find, and
says nothing, so a font that is not really there would carve the wrong
letters and report success.  ``tests/test_text_fonts.py`` measures every
font against the default to prove each one is drawn.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_TEXT_FONT",
    "TEXT_FONTS",
    "TextFont",
    "UnknownTextFontError",
    "list_text_fonts",
    "resolve_text_font",
]


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
    ),
    TextFont(
        id="mono",
        label="Mono",
        note="Even-width bold letters. Suits labels, part numbers and codes.",
        spec="Liberation Mono:style=Bold",
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
