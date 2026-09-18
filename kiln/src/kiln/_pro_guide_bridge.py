"""Public-Kiln -> kiln-pro maintenance-guide bridge.

The one place public Kiln asks kiln-pro for a maker's repair guide.  Every
door that walks a person through a repair -- the ``repair_guide`` tool,
``kiln repair``, the ``kiln doctor`` coverage line, the one sentence
``troubleshoot_printer`` adds when a fault code maps to a guide -- reaches
the table through here, so a guide is read in exactly one place and every
door says the same thing.

Why the guides are not in this repo.  A guide is the maker's own page read
closely and reproduced in the maker's ORDER (the power-off warning first,
then the steps, each with the maker's picture); the table of which pages
exist for which model, and which fault code starts which guide at which
step, is know-how Kiln has paid for.  Public Kiln keeps the mechanism
(this bridge, the door wiring, the step mode) and a tiny map of each
maker's public maintenance INDEX per model -- a link is a funnel, not a
leak -- so a public-only install still answers honestly.

Contract, the same as ``_pro_fault_bridge``: with no kiln-pro installed
every helper returns ``None`` cleanly, so a consumer branches on one value
and never on an import error.  Pictures are never fetched here or anywhere
in public Kiln: a guide carries the maker's URL and alt text, and the
maker's server serves the picture at answer time.  kiln-pro applies any
tier rule on ITS side (``guide_for_caller``); nothing in this file decides
a tier, and today nothing is paywalled.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def available() -> bool:
    """True when kiln-pro is installed and its guide reader imports."""
    try:
        import kiln_pro.device_intelligence.maintenance_guides  # noqa: F401

        return True
    except ImportError:
        return False


def _reader() -> Any | None:
    try:
        from kiln_pro.device_intelligence import maintenance_guides
    except ImportError:
        return None
    return maintenance_guides


def find_guide(printer_id: str, *, topic: str = "", code: str = "") -> dict[str, Any] | None:
    """The guide for *printer_id* that *topic* or *code* names, or ``None``.

    Returns ``{"slug", "guide", "start_at_step"}`` -- the guide THIS caller is
    entitled to (kiln-pro's ``guide_for_caller`` applies the rule, which
    today is "everyone").  ``None`` means: no kiln-pro, no guide for this
    model and topic/code, or a guide the caller's tier does not unlock; all
    three read the same to a door, which then answers with the maker's
    public maintenance index if it knows one.
    """
    reader = _reader()
    if reader is None or not printer_id:
        return None
    try:
        found = reader.find_guide(printer_id, topic=topic, code=code)
        if found is None:
            return None
        slug, _guide, start = found
        served = reader.guide_for_caller(slug)
    except Exception as exc:  # noqa: BLE001 -- a guide is never worth a crash
        logger.debug("kiln-pro guide lookup failed for %s/%s/%s: %s", printer_id, topic, code, exc)
        return None
    if not isinstance(served, dict):
        return None
    return {"slug": slug, "guide": served, "start_at_step": int(start)}


def guide_for_code(code: str, *, kind: str | None = None) -> tuple[str, int] | None:
    """``(guide slug, start_at_step)`` when a fault code maps to a guide, else ``None``."""
    reader = _reader()
    if reader is None or not code:
        return None
    try:
        return reader.guide_for_code(code, namespace=kind)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kiln-pro code->guide lookup failed for %s: %s", code, exc)
        return None


def coverage(printer_id: str) -> dict[str, Any] | None:
    """``{"maker", "count", "topics"}`` for one model, or ``None`` without kiln-pro."""
    reader = _reader()
    if reader is None or not printer_id:
        return None
    try:
        out = reader.coverage(printer_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kiln-pro guide coverage failed for %s: %s", printer_id, exc)
        return None
    return dict(out) if isinstance(out, dict) else None
