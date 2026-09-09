"""System font resolution — one candidate list, shared by every caller.

WHY THIS EXISTS
---------------
Three modules draw text onto a Pillow image, and all three used to
hand-maintain their own list of system font paths: :mod:`kiln.stage_paint`
(the plate stamp), :mod:`kiln.model_visualizer` (comparison-grid labels),
and :mod:`kiln.region_map` (header, legend and callouts).

The lists had already drifted apart.  stage_paint reached for
``HelveticaNeue.ttc`` first, model_visualizer for ``Helvetica.ttc`` and
then the bare name ``"Arial"``, region_map carried a regular list and a
bold one.  That drift is the whole problem: a copied list is a derived
value, and when a platform moves its fonts (or a new platform needs
adding) the fix lands in whichever copy someone was looking at.  The
other callers keep rendering a substitute face, which arrives looking
like a rendering bug rather than a missing font — the one failure nobody
thinks to grep for.

So the list lives here, once, and the callers ask.

TWO DOORS, BECAUSE CALLERS DEGRADE DIFFERENTLY
----------------------------------------------
:func:`load_font` always hands back something drawable, falling back to
Pillow's built-in face *at the size asked for*.  That is right for text
whose job is to be read: a plainer typeface still says what it says.

:func:`find_font` hands back ``None`` when the host has no real face.
That is right where a substitute face would be a lie rather than a
degradation — stage_paint repaints a stage that :mod:`kiln.stage_still`
photographs, and its calibration tests pin the result against real
photographs, so a mark set in a different typeface is drift from the
thing being matched.  Drawing nothing is the honest miss.

Both read the same candidates, so a path fixed here is fixed for
everyone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.ImageFont import FreeTypeFont
    from PIL.ImageFont import ImageFont as BitmapFont

    #: Either face Pillow hands back: a real scalable one, or — on a
    #: Pillow too old for sized defaults — its fixed-size bitmap face.
    Face = FreeTypeFont | BitmapFont

__all__ = ["find_font", "load_font"]

#: Regular-weight candidates, most-specific first.  The bare name at the
#: end is not a path: ``ImageFont.truetype`` searches the platform's own
#: font directories for it, which is worth one try before giving up on a
#: real face (it also catches a Windows installed somewhere other than C:).
_CANDIDATES_REGULAR: tuple[str, ...] = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "Arial",
)

#: Bold-weight candidates, in the order the three original lists used.
#:
#: Only the Linux and Windows entries are genuinely bold.  Both macOS
#: ``.ttc`` files open on their Regular face through this call — measured,
#: not assumed: HelveticaNeue.ttc reports style "Regular" and draws
#: slightly *lighter* than Helvetica.ttc.  Reaching a real bold on macOS
#: means selecting a face inside the collection, which none of the three
#: callers ever did.  The order is preserved rather than corrected so the
#: calibrated stage output does not move; making ``bold=True`` mean
#: something on macOS is a deliberate change, not a cleanup.
_CANDIDATES_BOLD: tuple[str, ...] = (
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "arialbd",
)


def find_font(size: int, *, bold: bool = False) -> Face | None:
    """The best available real face at *size*, or ``None`` if the host has none.

    Use this where there is a sensible way to draw nothing.  Callers that
    must draw something want :func:`load_font`.
    """
    from PIL import ImageFont

    for candidate in _CANDIDATES_BOLD if bold else _CANDIDATES_REGULAR:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
        except ImportError:
            # Pillow built without FreeType: no candidate can open, and
            # no later one will either.
            break
    return None


def load_font(size: int, *, bold: bool = False) -> Face:
    """The best available face at *size*, falling back to Pillow's built-in one.

    Always returns something drawable, and — this is the part that
    matters — the fallback is drawn at *size* too.

    ``load_default()`` answers at a fixed ~10 px however large a face was
    asked for.  A caller laying type out in several sizes (a title, a
    disclaimer, a legend) gets all of them back at 10 px, so the
    hierarchy that carried the meaning flattens into one unreadable row.
    Passing the size through leaves a fontless host looking like a
    plainer typeface instead of a broken render.
    """
    from PIL import ImageFont

    font = find_font(size, bold=bold)
    if font is not None:
        return font
    try:
        return ImageFont.load_default(size)
    except TypeError:
        # Pillow < 10.1: load_default takes no size.  A fixed ~10 px face
        # is all this host can offer.
        return ImageFont.load_default()
    except ImportError:
        # Pillow built without FreeType: the sized face needs it, the
        # legacy bitmap one does not.
        return ImageFont.load_default()
