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

A move Kiln sends on its own (a home, a park, a wipe, a purge, the park
before a filament change) is held to more: it is planned for the model the
printer was set up as, so it is refused while the printer names another
(:func:`move_refusal`).  Only a printer whose report is read off its own
serial number or firmware name is held to this, and only when both models
are in the catalogue; stopping, pausing, cancelling and status never ask.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from kiln.cli_command import kiln_command

logger = logging.getLogger(__name__)

#: The code a start or a move refused over the disagreement carries.
MISMATCH_CODE = "PRINTER_MODEL_DISAGREES"

#: Serial numbers, comma-separated, of printers whose model stands as it was
#: set up whatever the printer reports.  It is for an owner whose printer
#: Kiln's own records name wrongly, and it is theirs to set.
DECLARED_MODEL_STANDS_ENV = "KILN_DECLARED_MODEL_STANDS_FOR"


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
        # The row and the option are Kiln's own words; only the printer's
        # name is a value, quoted (a "--printer" handed over as a value would
        # be shown as a placeholder, since a leading dash reads as an option).
        line = (
            kiln_command(f"set-model {self.reported_row} --printer", printer_name)
            if printer_name else kiln_command("set-model", self.reported_row)
        )
        # A printer already set up as a model Kiln knows keeps it unless the
        # change is asked for by name.
        if self.declared_row:
            return f"`{line} --replace` (or the set_printer_model tool with replace=true)"
        return f"`{line}` (or the set_printer_model tool)"

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


def declared_model_stands(serial: object) -> bool:
    """Whether the owner has said the printer with *serial* is the model it was set up as."""
    own = str(serial or "").strip().upper()
    if not own:
        return False
    listed = os.environ.get(DECLARED_MODEL_STANDS_ENV, "")
    return own in {item.strip().upper() for item in listed.split(",")}


def move_refusal(adapter: Any) -> str | None:
    """The sentence that refuses a move Kiln plans by model, or ``None`` to let it run.

    A refusal needs proof: the adapter reads its report off the machine's own
    serial number or firmware name, that report and the model its moves are
    planned for (its ``printer_model``) each name a catalogue row, and the
    two rows differ.  No model set up, no report, a report or a model the
    catalogue has no row for, and a printer whose owner has said its model
    stands (:data:`DECLARED_MODEL_STANDS_ENV`) are all ``None``.  Never
    raises.
    """
    try:
        if not getattr(adapter, "_MODEL_REPORT_HOLDS_MOVES", False):
            return None
        planned_for = str(getattr(adapter, "_printer_model", "") or "").strip()
        if not planned_for:
            return None
        check = check_printer_model(adapter, declared=planned_for)
        if check.status != "differs" or check.declared_row is None:
            return None
        if declared_model_stands(getattr(adapter, "serial", "") or getattr(adapter, "_serial", "")):
            return None
        name = getattr(adapter, "_kiln_registered_name", None)
        name = name if isinstance(name, str) else ""
        return (
            f"Nothing was sent to {name or 'the printer'}.  It is set up as {check.declared_name}, but it "
            f"reports itself as {check.reported_name}.  The moves Kiln sends are planned for the model a "
            f"printer is set up as, and another printer's moves can drive the head into the bed or the "
            f"frame.  If it is {check.reported_name}, set it with {check.fix(name)} and try again; until "
            f"then, use the printer's own screen.  If it is {check.declared_name}, Kiln's record of this "
            "printer is wrong: say so with the report_issue tool."
        )
    except Exception:  # noqa: BLE001 -- a comparison that cannot run proves nothing
        logger.debug("move model check failed", exc_info=True)
        return None


__all__ = [
    "DECLARED_MODEL_STANDS_ENV",
    "MISMATCH_CODE",
    "ModelCheck",
    "check_printer_model",
    "declared_model_stands",
    "move_refusal",
]
