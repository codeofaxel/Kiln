"""The colour a made part is SHOWN in -- and asked to be PRINTED in.

An STL carries no colour, so a part made from code arrived on the stage grey
whatever the person asked for (2026-10-19: "make it teal", "orange PLA" --
grey both times, and the agent explained the grey away).  Grey is honest
about the PRINT: the part comes out in whatever filament is loaded.  It is
not honest about the PICTURE, which exists so the person can see what they
asked for.  So a make door records the colour it was asked for -- the
caller's word first, else the one colour the OpenSCAD source itself names --
and the preview and the stage show the part in it, while the block on the
result says plainly that this is how it is shown, not how it prints.

Then the print step keeps the promise.  The colour is written in a note
beside the file (``<file>.color.json``, bound to the file's bytes like the
arrival note), the slicer and the 3MF wrap carry it to the file that reaches
the printer, and the one AMS resolver every print door goes through
(``kiln.server._resolve_use_ams``) reads it: a loaded spool of that colour is
used and named; none loaded, and the print is HELD with the nearest spool
named and the two ways on (print that colour, or load the asked one); a
printer with no unit Kiln can read prints whatever is loaded and the result
says the colour was how the part was shown.  The person hears it before the
print, never after.

One source, not two: a source naming two different colours is a
multi-colour part, and painting all of it one of them would claim a colour
the file never stated, so nothing is picked.  Names resolve through the CSS
named colours -- the public standard OpenSCAD's ``color("...")`` takes --
and a kiln-pro resolver with house values for a few names falls back to it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: ``color("teal")`` / ``color('#2aa198')`` -- the string form OpenSCAD takes
#: (SVG names or hex).  The vector form ``color([r, g, b])`` is not read: it
#: is a number the person never said.
_COLOR_CALL_RE = re.compile(r"""\bcolor\s*\(\s*(["'])([^"']+)\1""")

#: What the colour block says about itself, in every result that carries one.
SHOWN_NOTE = (
    "How the part is shown on the preview and the stage; it prints in "
    "whatever filament is loaded."
)

#: Appended to the file's own path: ``part.stl`` -> ``part.stl.color.json``.
NOTE_SUFFIX = ".color.json"
SCHEMA_VERSION = 1

_HEX6_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX3_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")

#: The CSS/SVG named colours (CSS Color Module Level 4), lower-case name to
#: ``#rrggbb``.  A public standard, generated from a reference table rather
#: than typed; it is also the set of names OpenSCAD accepts in ``color()``.
CSS_NAMED_COLOURS: dict[str, str] = {
    "aliceblue": "#f0f8ff",
    "antiquewhite": "#faebd7",
    "aqua": "#00ffff",
    "aquamarine": "#7fffd4",
    "azure": "#f0ffff",
    "beige": "#f5f5dc",
    "bisque": "#ffe4c4",
    "black": "#000000",
    "blanchedalmond": "#ffebcd",
    "blue": "#0000ff",
    "blueviolet": "#8a2be2",
    "brown": "#a52a2a",
    "burlywood": "#deb887",
    "cadetblue": "#5f9ea0",
    "chartreuse": "#7fff00",
    "chocolate": "#d2691e",
    "coral": "#ff7f50",
    "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc",
    "crimson": "#dc143c",
    "cyan": "#00ffff",
    "darkblue": "#00008b",
    "darkcyan": "#008b8b",
    "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9",
    "darkgreen": "#006400",
    "darkgrey": "#a9a9a9",
    "darkkhaki": "#bdb76b",
    "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00",
    "darkorchid": "#9932cc",
    "darkred": "#8b0000",
    "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f",
    "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f",
    "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3",
    "deeppink": "#ff1493",
    "deepskyblue": "#00bfff",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "dodgerblue": "#1e90ff",
    "firebrick": "#b22222",
    "floralwhite": "#fffaf0",
    "forestgreen": "#228b22",
    "fuchsia": "#ff00ff",
    "gainsboro": "#dcdcdc",
    "ghostwhite": "#f8f8ff",
    "gold": "#ffd700",
    "goldenrod": "#daa520",
    "gray": "#808080",
    "green": "#008000",
    "greenyellow": "#adff2f",
    "grey": "#808080",
    "honeydew": "#f0fff0",
    "hotpink": "#ff69b4",
    "indianred": "#cd5c5c",
    "indigo": "#4b0082",
    "ivory": "#fffff0",
    "khaki": "#f0e68c",
    "lavender": "#e6e6fa",
    "lavenderblush": "#fff0f5",
    "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd",
    "lightblue": "#add8e6",
    "lightcoral": "#f08080",
    "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2",
    "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90",
    "lightgrey": "#d3d3d3",
    "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a",
    "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa",
    "lightslategray": "#778899",
    "lightslategrey": "#778899",
    "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0",
    "lime": "#00ff00",
    "limegreen": "#32cd32",
    "linen": "#faf0e6",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "mediumaquamarine": "#66cdaa",
    "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371",
    "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a",
    "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585",
    "midnightblue": "#191970",
    "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1",
    "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead",
    "navy": "#000080",
    "oldlace": "#fdf5e6",
    "olive": "#808000",
    "olivedrab": "#6b8e23",
    "orange": "#ffa500",
    "orangered": "#ff4500",
    "orchid": "#da70d6",
    "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98",
    "paleturquoise": "#afeeee",
    "palevioletred": "#db7093",
    "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9",
    "peru": "#cd853f",
    "pink": "#ffc0cb",
    "plum": "#dda0dd",
    "powderblue": "#b0e0e6",
    "purple": "#800080",
    "rebeccapurple": "#663399",
    "red": "#ff0000",
    "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1",
    "saddlebrown": "#8b4513",
    "salmon": "#fa8072",
    "sandybrown": "#f4a460",
    "seagreen": "#2e8b57",
    "seashell": "#fff5ee",
    "sienna": "#a0522d",
    "silver": "#c0c0c0",
    "skyblue": "#87ceeb",
    "slateblue": "#6a5acd",
    "slategray": "#708090",
    "slategrey": "#708090",
    "snow": "#fffafa",
    "springgreen": "#00ff7f",
    "steelblue": "#4682b4",
    "tan": "#d2b48c",
    "teal": "#008080",
    "thistle": "#d8bfd8",
    "tomato": "#ff6347",
    "turquoise": "#40e0d0",
    "violet": "#ee82ee",
    "wheat": "#f5deb3",
    "white": "#ffffff",
    "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00",
    "yellowgreen": "#9acd32",
}


def hex_for(shown: str | None) -> str | None:
    """``#rrggbb`` for a colour word or hex, or ``None`` when it is neither."""
    text = str(shown or "").strip()
    if not text:
        return None
    m = _HEX6_RE.match(text)
    if m:
        return "#" + m.group(1).lower()
    m = _HEX3_RE.match(text)
    if m:
        return "#" + "".join(c * 2 for c in m.group(1).lower())
    return CSS_NAMED_COLOURS.get(text.lower().replace(" ", "").replace("-", ""))


def single_scad_color(scad_code: str | None) -> str | None:
    """The one colour the source names, or ``None`` (none, or more than one)."""
    if not scad_code:
        return None
    names = {m.group(2).strip().lower() for m in _COLOR_CALL_RE.finditer(scad_code)}
    names.discard("")
    if len(names) != 1:
        return None
    return next(iter(names))


# ---------------------------------------------------------------------------
# The note beside the file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShownColour:
    """The colour a file was asked to be shown -- and printed -- in."""

    shown: str
    hex6: str
    source: str = "caller"

    @property
    def name(self) -> str:
        """The word the person used, else the hex."""
        return self.shown or self.hex6

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def note_path_for(file_path: str) -> str:
    return file_path + NOTE_SUFFIX


def _identity(file_path: str) -> str | None:
    """The file's content hash, as the print gate identifies a file."""
    from kiln.preview_gate import hash_file

    digest = hash_file(file_path)
    return None if digest.startswith("NO_FILE:") else digest


def record(file_path: str, colour: ShownColour) -> str | None:
    """Write the note for *file_path*.  Returns its path, or ``None`` when
    it could not be written.  Never raises: a make is never lost for the
    sake of its colour."""
    try:
        identity = _identity(file_path)
        if identity is None:
            return None
        note = note_path_for(file_path)
        body = {"schema": SCHEMA_VERSION, "file": identity, "colour": colour.to_dict()}
        with open(note, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        return note
    except Exception:  # noqa: BLE001 -- never costs the make
        logger.debug("colour note not written for %s", file_path, exc_info=True)
        return None


def read(file_path: str | None) -> ShownColour | None:
    """The note for *file_path*, or ``None`` when there is none, it is
    unreadable, or it describes different bytes than the file now holds."""
    if not file_path:
        return None
    try:
        with open(note_path_for(file_path), encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if body.get("file") != _identity(file_path):
            return None
        values = body.get("colour") or {}
        hex6 = hex_for(values.get("hex6"))
        if not hex6:
            return None
        return ShownColour(
            shown=str(values.get("shown") or hex6),
            hex6=hex6,
            source=str(values.get("source") or "caller"),
        )
    except (OSError, ValueError, TypeError):
        return None


def carry(from_path: str | None, to_path: str | None) -> None:
    """Give *to_path* the note of *from_path*: the sliced file, and the 3MF
    wrapped around it, are asked for the same colour as the part."""
    if not from_path or not to_path:
        return
    colour = read(from_path)
    if colour is not None:
        record(to_path, colour)


def forget(file_path: str | None) -> None:
    """Drop the note beside *file_path* (the person said: any colour)."""
    if not file_path:
        return
    try:
        import os

        os.remove(note_path_for(file_path))
    except OSError:
        pass


def attach_declared_color(
    response: dict[str, Any],
    color: str | None,
    *,
    scad_code: str | None = None,
    file_path: str | None = None,
) -> str | None:
    """Record the colour the part is to be shown in on ``response`` -- and,
    when ``file_path`` names the made file and the colour resolves, in the
    note beside it, so the print step can keep the promise.

    Returns the colour (the caller's ``color`` if given, else the single
    colour in ``scad_code``) so the door can hand it to its preview, or
    ``None`` when nothing was asked for.  Never raises.
    """
    try:
        said = str(color or "").strip()
        if said:
            shown, source = said, "caller"
        else:
            shown = single_scad_color(scad_code) or ""
            source = "scad"
        if not shown:
            return None
        block: dict[str, Any] = {"shown": shown, "source": source, "note": SHOWN_NOTE}
        hex6 = hex_for(shown)
        if hex6:
            block["hex"] = hex6
            if file_path:
                record(file_path, ShownColour(shown=shown, hex6=hex6, source=source))
        response["color"] = block
        return shown
    except Exception:  # noqa: BLE001 -- a display hint never costs a make
        return None
