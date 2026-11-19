"""Which printer a check is about when the caller named none.

A check that depends on the machine -- the bed a part must fit, the nozzle a
wall is judged for -- needs one printer.  This is the one place that picks
it, so every check picks the same one and can say which:

1. the printer the caller named;
2. the only printer Kiln knows of;
3. among several, on a plan that works with one printer at a time: the
   printer Kiln is working with, else the one set as the default;
4. among several, on a plan that runs printers side by side: none.  The
   check answers for each of them, or asks which one is meant.

Several printers with nothing to tell them apart is no answer either, and
the check asks.  During a call Kiln's servers run for another computer the
printers are that computer's (:mod:`kiln.callers_printers`); on those
servers with no such list, no printer is known.  Reads names only: nothing
is sent to a printer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: How a printer was chosen.
NAMED = "named"
ONLY = "only"
WORKING_WITH = "working_with"
DEFAULT = "default"
SEVERAL = "several"
NONE = "none"

_WHY = {
    ONLY: "the only printer Kiln knows of",
    WORKING_WITH: "the printer Kiln is working with",
    DEFAULT: "your default printer",
}


@dataclass(frozen=True)
class PrinterChoice:
    """One answer.

    :param printer: The printer to check for, or ``None`` when none was
        chosen.
    :param how: :data:`NAMED`, :data:`ONLY`, :data:`WORKING_WITH`,
        :data:`DEFAULT`, :data:`SEVERAL` (several known, none chosen) or
        :data:`NONE` (none known).
    :param machines: Every printer Kiln knows of, one name each.
    :param every_printer: Several are known, none was named, and the plan
        runs them side by side: answer for each, or ask.
    """

    printer: str | None
    how: str
    machines: tuple[str, ...] = ()
    every_printer: bool = False

    @property
    def inferred(self) -> bool:
        """Nobody named the printer and one was chosen."""
        return self.how in _WHY

    @property
    def why(self) -> str:
        """Why this printer, for a sentence that already names it; ``""``
        when the caller named it or none was chosen."""
        return _WHY.get(self.how, "")

    @property
    def said(self) -> str:
        """The chosen printer and why it was chosen; ``""`` when the caller
        named it or none was chosen."""
        return f"{self.printer}, {self.why}" if self.inferred else ""

    @property
    def ask(self) -> str:
        """What to say when several are known and none was chosen."""
        if self.how != SEVERAL:
            return ""
        names = ", ".join(self.machines)
        return f"Kiln knows of {len(self.machines)} printers ({names}) and none was named: name the one this is for."

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "how": self.how,
            "printers": list(self.machines),
            "note": self.said or self.ask,
        }


def known_machines() -> list[str]:
    """The printers Kiln knows of, one name per machine: the caller's during
    a call Kiln's servers run for another computer, else the ones registered
    here.  None on those servers otherwise."""
    from kiln import callers_printers

    if callers_printers.known():
        return callers_printers.machines()
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return []
        from kiln.registry import get_printer_registry

        return [str(name) for name in get_printer_registry().list_machines()]
    except Exception:  # noqa: BLE001 -- no registry is no machine
        return []


def _working_with(machines: list[str]) -> str | None:
    """The machine among *machines* Kiln is working with, or ``None``."""
    from kiln import callers_printers

    if callers_printers.known():
        return None
    try:
        from kiln.printers import engagement
        from kiln.registry import get_printer_registry

        held = engagement.current()
        if held is None:
            return None
        registry = get_printer_registry()
        for name in machines:
            if engagement.machine_id(registry.get(name)) == held.machine:
                return name
    except Exception:  # noqa: BLE001 -- a record that cannot be read picks nothing
        logger.debug("which printer: engagement not read", exc_info=True)
    return None


def _default(machines: list[str]) -> str | None:
    """The machine among *machines* set as the default, or ``None``."""
    from kiln import callers_printers

    if callers_printers.known():
        chosen = [p for p in callers_printers.printers() if p.get("is_default")]
        if len(chosen) != 1:
            return None
        name = str(chosen[0].get("same_machine_as") or chosen[0].get("name") or "")
        return name if name in machines else None
    try:
        from kiln.registry import get_printer_registry

        registry = get_printer_registry()
        names = set(registry.names_for(registry.get("default")))
    except Exception:  # noqa: BLE001 -- no default registered
        return None
    held = [name for name in machines if name in names]
    return held[0] if len(held) == 1 else None


def _runs_printers_side_by_side() -> bool:
    try:
        from kiln.printers.engagement import multi_machine_tier

        return multi_machine_tier()
    except Exception:  # noqa: BLE001 -- an unreadable plan is the one-printer plan
        return False


def which_printer(
    named: str | None = None,
    *,
    machines: list[str] | None = None,
    side_by_side: bool | None = None,
) -> PrinterChoice:
    """The printer a check is about: *named* when the caller named one, else
    chosen by the module's rule.  Never raises.

    *machines* is the list to choose among when the caller already holds
    it; left out, :func:`known_machines` is asked.  *side_by_side* is whether
    the caller's plan runs printers side by side, when the caller has
    already read it; left out, the plan is read here.
    """
    asked = (named or "").strip()
    known = tuple(known_machines() if machines is None else machines)
    if asked:
        return PrinterChoice(asked, NAMED, known)
    if not known:
        return PrinterChoice(None, NONE)
    if len(known) == 1:
        return PrinterChoice(known[0], ONLY, known)
    if _runs_printers_side_by_side() if side_by_side is None else side_by_side:
        return PrinterChoice(None, SEVERAL, known, every_printer=True)
    held = _working_with(list(known))
    if held is not None:
        return PrinterChoice(held, WORKING_WITH, known)
    default = _default(list(known))
    if default is not None:
        return PrinterChoice(default, DEFAULT, known)
    return PrinterChoice(None, SEVERAL, known)


__all__ = [
    "DEFAULT", "NAMED", "NONE", "ONLY", "SEVERAL", "WORKING_WITH",
    "PrinterChoice", "known_machines", "which_printer",
]
