"""Is the printer the model its owner said it is?

Two answers: the model declared at setup
(:meth:`kiln.printers.base.PrinterAdapter.declared_printer_model`) and the
model the printer reports about itself
(:meth:`kiln.printers.base.PrinterAdapter.get_printer_info`: a Bambu Lab
serial number or firmware name, Prusa Link's printer type, an Elegoo's
machine name, a Marlin board's ``M115``).  When both name catalogue rows and
the rows differ, one of them is wrong.  When either cannot say, nothing is
claimed: ``unchecked`` is never ``match``.

The declaration still owns every limit (the safety boundary on
``get_printer_info``): nothing here replaces it.  What this adds is one
comparison every door reads -- the print-start gate, which also judges a part
against the bed of the printer that answered so a model typed wrong at setup
cannot start a part bigger than the real bed, and the setup doors, which say
the disagreement the moment it is made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kiln.cli_command import kiln_command

logger = logging.getLogger(__name__)

#: The code a start refused over the disagreement carries.
MISMATCH_CODE = "PRINTER_MODEL_DISAGREES"


@dataclass(frozen=True)
class ModelCheck:
    """One comparison.

    :param status: ``"match"``, ``"differs"``, or ``"unchecked"`` (*why*
        says which side could not answer).
    :param declared: The model as the owner declared it.
    :param declared_row: The catalogue row the declaration names, or
        ``None`` for a printer Kiln has no record for.
    :param reported_row: The catalogue row the printer's own report names.
    :param reported_raw: What the printer actually said, before mapping.
    """

    status: str
    declared: str = ""
    declared_row: str | None = None
    reported_row: str | None = None
    reported_raw: str | None = None
    why: str | None = None

    @property
    def declared_name(self) -> str:
        """The declared model as a person reads it: the row's own name, or
        the model as typed for a printer outside the catalogue."""
        return f"the {_display(self.declared_row)}" if self.declared_row else repr(self.declared)

    @property
    def reported_name(self) -> str:
        return f"the {_display(self.reported_row)}"

    def fix(self, printer_name: str = "") -> str:
        """The one command that sets the printer to the model it reports."""
        target = ("--printer", printer_name) if printer_name else ()
        return f"`{kiln_command('set-model', self.reported_row, *target)}` (or the set_printer_model tool)"

    def sentence(self, printer_name: str = "") -> str:
        """The disagreement in words, said at setup, with the command that
        settles it and what Kiln does meanwhile."""
        if self.status != "differs":
            return ""
        if self.declared_row is None:
            # A printer outside the catalogue may run on its owner's own bed
            # and limits, so nothing here says which limits it is held to.
            return (
                f"This printer reports itself as {self.reported_name}, and it was set up as "
                f"{self.declared_name}, which is not in Kiln's catalogue.  If it is {self.reported_name}, "
                f"set it with {self.fix(printer_name)} and Kiln works to that printer's limits; until "
                f"then Kiln will not start a part too big for {self.reported_name}'s bed."
            )
        return (
            f"This printer was set up as {self.declared_name}, but it reports itself as "
            f"{self.reported_name}.  Kiln holds prints to {self.declared_name}'s limits, as set up, and "
            f"will not start a part too big for {self.reported_name}'s bed while the two disagree.  If "
            f"the printer is right, set it with {self.fix(printer_name)}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "declared": self.declared,
            "declared_row": self.declared_row,
            "reported_row": self.reported_row,
            "reported_raw": self.reported_raw,
            "why": self.why,
        }


def _display(row: str | None) -> str:
    """The row's own name as the catalogue writes it, else the row id."""
    try:
        from kiln.printers.bed_fit import _load_printer_intelligence

        entry = (_load_printer_intelligence() or {}).get(row or "")
        if isinstance(entry, dict) and entry.get("display_name"):
            return str(entry["display_name"])
    except Exception:  # noqa: BLE001 -- a name is help, never a reason to fail
        logger.debug("printer model check: no display name for %r", row, exc_info=True)
    return str(row or "")


def _row(spelling: str | None) -> str | None:
    """The catalogue row *spelling* names, by the resolver every door reads."""
    if not spelling:
        return None
    from kiln.catalog_keys import resolve_printer_key
    from kiln.motion_facts import catalogue_keys

    return resolve_printer_key(spelling, catalogue_keys())


def check_printer_model(adapter: Any, *, declared: str | None = None) -> ModelCheck:
    """Compare *adapter*'s declared model with the printer's own report.

    *declared* stands in for the adapter's declaration when a door is about
    to change it (the model being set is what the printer is compared
    with).  ``unchecked`` whenever nobody declared a model, the printer
    reports nothing, or its report names no catalogue row.  Costs at most
    the one bounded read ``get_printer_info`` makes.  Never raises.
    """
    try:
        said = (declared if declared is not None else adapter.declared_printer_model()) or ""
        said = said.strip() if isinstance(said, str) else ""
        if not said:
            return ModelCheck("unchecked", why="no model was declared")
        info = adapter.get_printer_info()
        reported_raw = getattr(info, "raw_model", None) or getattr(info, "model", None)
        reported_row = _row(getattr(info, "model", None))
        if reported_row is None:
            why = "the printer reports no model" if info is None else "its report names no catalogue row"
            return ModelCheck("unchecked", declared=said, reported_raw=reported_raw, why=why)
        declared_row = _row(said)
        status = "match" if declared_row == reported_row else "differs"
        return ModelCheck(status, said, declared_row, reported_row, reported_raw)
    except Exception:  # noqa: BLE001 -- a comparison never costs a setup or a start
        logger.debug("printer model check failed", exc_info=True)
        return ModelCheck("unchecked", why="the comparison could not run")


__all__ = ["MISMATCH_CODE", "ModelCheck", "check_printer_model"]
