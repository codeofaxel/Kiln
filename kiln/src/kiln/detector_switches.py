"""What a machine says about its own detectors' switches, whatever its brand.

A printer's detector can be present, documented, and switched OFF on the
machine's own screen.  Everything Kiln says about what is watching a print
is otherwise composed from research about the MODEL, so the one case where
the research and the machine in the room disagree is exactly the case an
owner is most likely to be misled by — they walk away because a detector
they turned off last month is listed as present.

This asks the machine instead, through the adapter contract every backend
shares, and answers in three states.  ``True`` is on, ``False`` is off, and
``None`` is "nobody could verify it" -- never ``False``, because a reading
that failed and a switch that is off look identical in a summary and only
one of them is a reason to keep watching the print yourself.  A machine
that has no such setting, a backend that cannot report one, and a printer
that is not reachable all say nothing at all rather than guess.

The keys are Kiln's own nouns for the settings, the ones the status read
uses; a consumer maps them to whatever it calls its detectors.  One helper,
so a second brand's switch joins here and reaches every door at once
instead of growing a per-door branch.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["detector_switches"]


def detector_switches(adapter: Any) -> dict[str, bool | None]:
    """Every detector switch *adapter* can report, keyed by Kiln's own noun.

    Never raises and never guesses: an unreachable printer, an older backend
    with no such contract method, and a machine whose maker states no such
    setting all return no entry for it.
    """
    switches: dict[str, bool | None] = {}
    if adapter is None:
        return switches

    from kiln.nozzle_clumping_detection import read_switch

    reading = read_switch(adapter)
    # ``supported is False`` is the machine saying it HAS no such setting,
    # which is a fact about the model and not a switch state; the coverage
    # research already carries it, and answering "unknown" here would argue
    # with it.
    if reading is not None and reading.supported is not False:
        switches["nozzle_clumping_detection"] = reading.enabled
    return switches
