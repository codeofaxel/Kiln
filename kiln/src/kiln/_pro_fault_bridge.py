"""Public-Kiln -> kiln-pro fault-reading bridge.

The one place public Kiln asks kiln-pro what a printer's fault code means.
Every door that shows a fault -- ``printer_status``'s ``fault_note`` and
``fault_remedy``, the load / unload / purge results, the fault event on the
bus, ``troubleshoot_printer``'s code lookup -- reaches the reading through
``kiln.printers.bambu.read_bambu_fault``, and that function asks here first.
So a reading lives in exactly one place and every door says the same thing.

Why the readings are not in this repo.  A fault code's cause and fix are
know-how Kiln has paid for (a vendor page read closely, an owner thread
where the real fix was found, a machine on the bench).  Where know-how
lives (the repo) and who pays for it (the tier) are two different axes:
kiln-pro holds the reading, and decides per row whether it is free -- a
safety or fix floor is never paywalled -- or paid depth.  Public Kiln keeps
the mechanism (this bridge, the door wiring) and a family line that says
what kind of fault it is and where the reading is.

Contract, the same as ``_pro_nozzle_bridge``: with no kiln-pro installed
every helper returns ``None`` cleanly, so a consumer branches on one value
and never on an import error.  kiln-pro applies the tier rule on ITS side
(``decode_for_caller``), so a reading that comes back here is one the
caller is entitled to; nothing in this file decides a tier.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def available() -> bool:
    """True when kiln-pro is installed and its fault catalog imports."""
    try:
        import kiln_pro.device_intelligence.hms_catalog  # noqa: F401

        return True
    except ImportError:
        return False


def decode_fault(code: str, *, kind: str = "print_error") -> dict[str, Any] | None:
    """kiln-pro's reading of a Bambu fault code, or ``None``.

    *kind* names which of Bambu's two fault fields the code came out of
    (``"print_error"`` or ``"hms"``), because the same eight digits mean
    different things in each and kiln-pro refuses to answer for the wrong
    one.  The dict, when there is one, carries at least ``cause`` and
    ``fix`` and never a URL -- the served shape is allowlisted on the
    kiln-pro side.  ``None`` means: no kiln-pro, no row for this code, or a
    row the caller's tier does not unlock.  All three read the same to a
    door, which then falls back to public Kiln's own family line.
    """
    if not code or not isinstance(code, str):
        return None
    try:
        from kiln_pro.device_intelligence.hms_catalog import decode_for_caller
    except ImportError:
        return None
    try:
        decoded = decode_for_caller(code, namespace=kind)
    except Exception as exc:  # noqa: BLE001 -- a reading is never worth a crash
        logger.debug("kiln-pro fault decode failed for %s: %s", code, exc)
        return None
    if not isinstance(decoded, dict) or not decoded.get("cause"):
        return None
    return decoded
