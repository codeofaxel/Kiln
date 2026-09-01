"""System font resolution — one candidate list, shared by every caller.

WHY THIS EXISTS
---------------
Three modules used to draw text onto a Pillow image, and all three
hand-maintained their own list of system font paths to try before
falling back to PIL's built-in face: :mod:`kiln.stage_paint` (the plate
stamp), :mod:`kiln.model_visualizer` (comparison-grid labels), and
:mod:`kiln.region_map` (header, legend and callouts).

The lists had already drifted apart.  stage_paint reached for
``HelveticaNeue.ttc`` first, model_visualizer for ``Helvetica.ttc`` and
then the bare name ``"Arial"``, region_map carried a regular list and a
bold one.  That drift is the whole problem: a copied list is a derived
value, and when a platform moves its fonts (or a new platform needs
adding) the fix lands in whichever copy someone was looking at.  The
other callers keep silently rendering PIL's tiny bitmap face, which
arrives looking like a rendering bug rather than a missing font — the
one failure nobody thinks to grep for.

So the list lives here, once, and the callers ask.

TWO DOORS, BECAUSE CALLERS DEGRADE DIFFERENTLY
----------------------------------------------
:func:`load_font` always hands back something drawable, falling back to
``ImageFont.load_default()``.  That is right for labels: a small ugly
label still reads.

:func:`find_font` hands back ``None`` when the host has no real face.
That is right for decorative text sized in the image's own units —
stage_paint's plate stamp is laid out against a font size derived from
the texture resolution, so PIL's fixed-size bitmap face would paint an
illegible smudge in the corner where the honest answer is to draw
nothing at all.

Both read the same candidates, so a path fixed here is fixed for
everyone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["find_font", "load_font"]

#: Regular-weight candidates, most-specific first.  The bare names at the
#: end are not paths: ``ImageFont.truetype`` searches the platform's own
#: font directories for them, which is the last thing worth trying before
#: giving up on a real face.
_CANDIDATES_REGULAR: Sequence[str] = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "Arial",
)

#: Bold-weight candidates.  Same ordering rule; HelveticaNeue leads
#: because its ``.ttc`` opens on a bolder face than Helvetica's does.
_CANDIDATES_BOLD: Sequence[str] = (
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "arialbd",
)


def find_font(size: int, *, bold: bool = False) -> Any | None:
    """The best available real face at *size*, or ``None`` if the host has none.

    Use this when there is a sensible way to draw nothing.  Callers that
    must draw something want :func:`load_font`.
    """
    from PIL import ImageFont

    for candidate in _CANDIDATES_BOLD if bold else _CANDIDATES_REGULAR:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return None


def load_font(size: int, *, bold: bool = False) -> Any:
    """The best available face at *size*, falling back to PIL's built-in one.

    Always returns something drawable.  The fallback ignores *size* —
    that is PIL's contract, not a bug here — so text laid out against
    *size* will not fit it.  Callers that care want :func:`find_font`.
    """
    from PIL import ImageFont

    font = find_font(size, bold=bold)
    if font is None:
        return ImageFont.load_default()
    return font
