"""The plate the 3D stage draws under a model.

WHY THIS EXISTS
---------------
Kiln's stage stands every design on a print bed.  The bed is not decoration:
it is the only thing in the frame with a known size, so it is what tells a
person whether the part in front of them is a coaster or a shelf — and, when
the part hangs off the edge, that it will not print in one piece.

The stage had no way to know how big the bed was, so it drew a 256 mm square
for everyone.  On a 350 mm machine that understates the room the user has; on
a 450 mm part it draws a small dark square that disappears under the model and
reads as an artifact rather than a plate.  This module answers the one
question the stage could not: **how big is this install's bed, and whose is
it?**

WHAT IT RETURNS
---------------
A plain dict, ready to ride the ``kiln.mesh.v1`` payload as ``plate``::

    {"x_mm": 256.0, "y_mm": 256.0, "z_mm": 256.0,
     "printer_id": "bambu_a1", "label": "Bambu Lab A1", "source": "printer"}

``source`` is the honesty field, and every consumer keys off it:

* ``"printer"`` — these are a real machine's dimensions, from
  ``printer_intelligence.json`` via the printer model in ``config.yaml``.
  The stage may etch the name on the plate and may draw the machine's build
  envelope, because both are claims about a bed we actually know.
* ``"default"`` — nobody told us which printer this is, so the stage draws a
  reference plate.  It says nothing about anyone's machine, and the stage
  must not decorate it with a name or a volume.

TWO PLACES IT DELIBERATELY STAYS QUIET
--------------------------------------
* **No printer model configured.**  ``config.yaml`` carries ``printer_model``
  or it does not; this module never infers one from a serial prefix or a
  hostname (see :mod:`kiln.printer_model_resolver` for why that inference was
  removed).  Unknown means the reference plate, not a guess.
* **The hosted server.**  One process there serves every customer out of one
  ``~/.kiln``, so that file's ``printer_model`` is not the caller's — it is
  whatever the box happens to have.  Resolution is skipped entirely, and
  every hosted caller gets the reference plate.

Never raises: a stage that cannot name the bed still has to draw one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Edge length of the reference plate, in millimetres.  Sits near the middle
#: of consumer FDM beds (Ender 235, Prusa 250, Bambu 256) so an unknown
#: machine is neither flattered nor shortchanged.  The stage carries the same
#: number as its own fallback — this one is what the payload states when it
#: states anything at all.
DEFAULT_PLATE_MM = 256.0


def _display_name(printer_id: str) -> str | None:
    """Catalogue name for an already-canonical printer id, or ``None``.

    Exact match only.  The fuzzy lookups elsewhere fall back to a ``default``
    profile, which is fine for settings advice and wrong here: a plate etched
    with the wrong printer's name is worse than a plate etched with nothing.
    """
    try:
        from kiln.printers.bed_fit import get_printer_display_name

        return get_printer_display_name(printer_id)
    except Exception:  # noqa: BLE001 — a missing name is not a failure
        return None


def default_stage_plate() -> dict[str, Any]:
    """The reference plate — a square of :data:`DEFAULT_PLATE_MM`, unattributed."""
    return {
        "x_mm": DEFAULT_PLATE_MM,
        "y_mm": DEFAULT_PLATE_MM,
        "z_mm": None,
        "printer_id": None,
        "label": None,
        "source": "default",
    }


def resolve_stage_plate(printer_id: str | None = None) -> dict[str, Any]:
    """Resolve the plate for this install (or for an explicit *printer_id*).

    Falls back to :func:`default_stage_plate` for every unknown: no printer
    model configured, a model the catalogue does not carry, a hosted process,
    or any error at all along the way.
    """
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if printer_id is None and is_hosted_multitenant():
            # One shared ~/.kiln, many customers — its printer is nobody's.
            return default_stage_plate()

        if printer_id is None:
            from kiln.printer_model_resolver import resolve_printer_model

            printer_id = resolve_printer_model()
        if not printer_id:
            return default_stage_plate()

        from kiln.printers.bed_fit import resolve_build_volume

        resolved = resolve_build_volume(printer_id)
        if not resolved:
            return default_stage_plate()
        canonical, (x, y, z) = resolved
        return {
            "x_mm": float(x),
            "y_mm": float(y),
            "z_mm": float(z),
            "printer_id": canonical,
            "label": _display_name(canonical),
            "source": "printer",
        }
    except Exception:  # noqa: BLE001 — the stage must never fail on furniture
        logger.debug("stage plate not resolved", exc_info=True)
        return default_stage_plate()


def attach_stage_plate(
    payload: dict[str, Any] | None, printer_id: str | None = None
) -> dict[str, Any] | None:
    """Stamp the resolved plate onto a ``kiln.mesh.v1`` *payload*, in place.

    The single call every payload-producing door makes, so a door added later
    cannot ship a stage with no bed under it.  A ``None`` payload (no geometry
    to show) passes straight through.
    """
    if not isinstance(payload, dict):
        return payload
    payload["plate"] = resolve_stage_plate(printer_id)
    return payload
