"""The colour a made part is SHOWN in -- a display hint, never a print setting.

An STL carries no colour, so a part made from code arrived on the stage grey
whatever the person asked for (2026-10-19: "make it teal", "orange PLA" --
grey both times, and the agent explained the grey away).  Grey is honest
about the PRINT: the part comes out in whatever filament is loaded.  It is
not honest about the PICTURE, which exists so the person can see what they
asked for.  So a make door records the colour it was asked for -- the
caller's word first, else the one colour the OpenSCAD source itself names --
and the preview and the stage show the part in it, while the block on the
result says plainly that this is how it is shown, not how it prints.

One source, not two: a source naming two different colours is a
multi-colour part, and painting all of it one of them would claim a colour
the file never stated, so nothing is picked.
"""

from __future__ import annotations

import re
from typing import Any

#: ``color("teal")`` / ``color('#2aa198')`` -- the string form OpenSCAD takes
#: (SVG names or hex).  The vector form ``color([r, g, b])`` is not read: it
#: is a number the person never said.
_COLOR_CALL_RE = re.compile(r"""\bcolor\s*\(\s*(["'])([^"']+)\1""")

#: What the colour block says about itself, in every result that carries one.
SHOWN_NOTE = (
    "How the part is shown on the preview and the stage; it prints in "
    "whatever filament is loaded."
)


def single_scad_color(scad_code: str | None) -> str | None:
    """The one colour the source names, or ``None`` (none, or more than one)."""
    if not scad_code:
        return None
    names = {m.group(2).strip().lower() for m in _COLOR_CALL_RE.finditer(scad_code)}
    names.discard("")
    if len(names) != 1:
        return None
    return next(iter(names))


def attach_declared_color(
    response: dict[str, Any], color: str | None, *, scad_code: str | None = None
) -> str | None:
    """Record the colour the part is to be shown in on ``response``.

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
        response["color"] = {"shown": shown, "source": source, "note": SHOWN_NOTE}
        return shown
    except Exception:  # noqa: BLE001 -- a display hint never costs a make
        return None
