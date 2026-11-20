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

The printer's owner can settle a disagreement the other way, for that one
machine: they say it is the model it was set up as, or that an upgrade its
maker sells (:func:`maker_upgrades`) is fitted.  Only a person makes that
statement, through the proof a print start takes (a code shown on this
computer's screen, or a person at a terminal); it is kept on the printer's
own saved record with the serial number it was made for
(:data:`STATEMENT_FIELD`), shown by the setup check, and taken back by
setting the model to what the printer reports.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from typing import Any

from kiln.cli_command import kiln_command

logger = logging.getLogger(__name__)

#: The code a start or a move refused over the disagreement carries.
MISMATCH_CODE = "PRINTER_MODEL_DISAGREES"

#: The field of a saved printer's record that holds its owner's statement:
#: ``serial`` (the machine it was made for), ``model`` (what the owner says
#: it is), ``upgrade`` (a key of :func:`maker_upgrades`, when that is why),
#: ``said_at``, ``how`` (the proof the person gave) and ``by``.
STATEMENT_FIELD = "owner_says"

#: The data file listing the upgrades printer makers sell, one row each.
_UPGRADES_FILE = "maker_upgrades.json"
_READERS = ("slicing", "limits", "moves")
_FOLLOWS = ("becomes", "fitted_to")


@functools.lru_cache(maxsize=1)
def maker_upgrades() -> dict[str, dict[str, Any]]:
    """Every well-formed row of the upgrades file, by upgrade id.

    A row names the model it is ``fitted_to`` and either the model it
    ``becomes`` or what it ``adds``; ``reports_itself`` (true, false or
    null for not known); and what each reader (slicing, limits, moves)
    ``follows``.  A row with any other shape is left out and logged, never
    guessed at.
    """
    from kiln.data_manifest import DATA_ROOT

    try:
        raw = json.loads((DATA_ROOT / _UPGRADES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("printer model check: %s could not be read", _UPGRADES_FILE, exc_info=True)
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for upgrade, row in raw.items():
        if upgrade.startswith("_"):
            continue
        follows = row.get("follows") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or not all(isinstance(row.get(k), str) and row[k] for k in ("name", "fitted_to"))
            or (isinstance(row.get("becomes"), str) and bool(row["becomes"])) == (isinstance(row.get("adds"), str) and bool(row["adds"]))
            or row.get("reports_itself") not in (True, False, None)
            or not isinstance(follows, dict)
            or set(follows) != set(_READERS)
            or any(v not in _FOLLOWS for v in follows.values())
        ):
            logger.warning("printer model check: upgrade %r is not a well-formed row; it is left out", upgrade)
            continue
        rows[upgrade] = row
    return rows


def _statement_upgrades() -> dict[str, dict[str, Any]]:
    """The upgrades an owner's statement can rest on: the printer becomes
    another model, does not say so itself, and Kiln uses that model's
    records for slicing, limits and moves."""
    return {
        upgrade: row for upgrade, row in maker_upgrades().items()
        if row.get("becomes") and row["reports_itself"] is not True
        and all(row["follows"][reader] == "becomes" for reader in _READERS)
    }


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


def _same_serial(one: object, other: object) -> bool:
    mine, theirs = str(one or "").strip().upper(), str(other or "").strip().upper()
    return bool(mine) and mine == theirs


def entry_statement(entry: Any) -> dict[str, Any] | None:
    """The owner's statement on one saved printer's record, while it is still about that record.

    It names the printer's own serial number, and the model it names is the
    model the record is set up as now: a statement about a model the
    printer has since been set up as something else is not read.
    """
    said = entry.get(STATEMENT_FIELD) if isinstance(entry, dict) else None
    if not isinstance(said, dict) or not _same_serial(said.get("serial"), entry.get("serial")):
        return None
    stated = _row(said.get("model"))
    return said if stated is not None and stated == _row(entry.get("printer_model")) else None


def owner_statement(serial: object) -> dict[str, Any] | None:
    """The statement on the saved printer whose serial number is *serial*, or ``None``.  Never raises."""
    if not str(serial or "").strip():
        return None
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        printers = _read_config_file(get_config_path()).get("printers")
    except Exception:  # noqa: BLE001 -- no record read is no statement
        logger.debug("printer model check: saved printers unreadable", exc_info=True)
        return None
    for entry in (printers or {}).values() if isinstance(printers, dict) else ():
        said = entry_statement(entry)
        if said is not None and _same_serial(said.get("serial"), serial):
            return said
    return None


def upgrade_between(reported_row: str | None, declared_row: str | None) -> str | None:
    """The maker's upgrade that makes *reported_row* into *declared_row*, or ``None``."""
    for upgrade, row in _statement_upgrades().items():
        if (_row(row["fitted_to"]), _row(row["becomes"])) == (reported_row, declared_row):
            return upgrade
    return None


def statement_covers(said: Any, declared_row: str | None, reported_row: str | None) -> bool:
    """Whether *said* is the owner's word for exactly this pairing: set up as *declared_row* while the printer reports *reported_row*."""
    if not isinstance(said, dict) or declared_row is None or _row(said.get("model")) != declared_row:
        return False
    upgrade = said.get("upgrade")
    return not upgrade or upgrade_between(reported_row, declared_row) == upgrade


def _serial_of(adapter: Any) -> str:
    return str(getattr(adapter, "serial", "") or getattr(adapter, "_serial", "") or "")


def reported_row(adapter: Any) -> str | None:
    """The catalogue model *adapter*'s machine names itself, from the report a move is held to, or ``None``.

    Read the way :func:`move_refusal` reads it: only on a backend whose
    report comes off the machine's own serial number or firmware name, and
    only when that report names one catalogue model.  Never raises.
    """
    if not getattr(adapter, "_MODEL_REPORT_HOLDS_MOVES", False):
        return None
    try:
        return _row(getattr(adapter.get_printer_info(), "model", None))
    except Exception:  # noqa: BLE001 -- a report that cannot be read names nothing
        logger.debug("printer model check: no report read", exc_info=True)
        return None


def stated_upgrade(adapter: Any) -> str:
    """The maker's upgrade the owner recorded for *adapter*'s machine, while it is set up as the model the upgrade makes it; else ``""``."""
    try:
        said = owner_statement(_serial_of(adapter)) or {}
        upgrade = str(said.get("upgrade") or "")
        row = _statement_upgrades().get(upgrade)
        planned_for = _row(str(getattr(adapter, "_printer_model", "") or ""))
        if row is not None and planned_for == _row(row["becomes"]) == _row(said.get("model")):
            return upgrade
    except Exception:  # noqa: BLE001 -- a statement that cannot be read is none
        logger.debug("printer model check: stated upgrade unreadable", exc_info=True)
    return ""


def _statement_door(model: str, printer_name: str) -> str:
    line = (
        kiln_command(f"set-model {model} --printer", printer_name) if printer_name
        else kiln_command("set-model", model)
    )
    return (
        f"`{line} --owner-says-so` at a terminal, or the set_printer_model tool with owner_says_so=true "
        "and the code Kiln then shows on this computer's screen"
    )


def settle(check: ModelCheck, printer_name: str = "") -> str:
    """How a disagreement is settled: set the printer as what it reports, or its owner says the setting is right."""
    usual = f"Set it as the printer it reports: {check.fix(printer_name)}."
    door = _statement_door(str(check.declared_row or check.declared), printer_name)
    upgrade = upgrade_between(check.reported_row, check.declared_row)
    if upgrade is not None:
        return f"{usual}  If {maker_upgrades()[upgrade]['name']} is fitted, its owner can record that with {door}."
    return f"{usual}  If it really is {check.declared_name}, its owner can say so with {door}."


def statement_text(said: dict[str, Any], printer_name: str) -> str:
    """What the owner's statement says, in the words a person confirms."""
    upgrade = said.get("upgrade")
    model = f"the {_display(_row(said.get('model')))}"
    row = _statement_upgrades().get(str(upgrade or ""))
    if row is not None:
        return f"{row['name']} is fitted to {printer_name}, so it is {model}"
    return f"{printer_name} is {model}, whatever its serial number says"


def _moves_disagreement(adapter: Any) -> tuple[ModelCheck, dict[str, Any] | None] | None:
    """The proven disagreement a move is held to, with the owner's statement that settles it, or ``None``."""
    if not getattr(adapter, "_MODEL_REPORT_HOLDS_MOVES", False):
        return None
    planned_for = str(getattr(adapter, "_printer_model", "") or "").strip()
    if not planned_for:
        return None
    check = check_printer_model(adapter, declared=planned_for)
    if check.status != "differs" or check.declared_row is None:
        return None
    said = owner_statement(_serial_of(adapter))
    return check, (said if statement_covers(said, check.declared_row, check.reported_row) else None)


def move_refusal(adapter: Any) -> str | None:
    """The sentence that refuses a move Kiln plans by model, or ``None`` to let it run.

    A refusal needs proof: the adapter reads its report off the machine's own
    serial number or firmware name, that report and the model its moves are
    planned for (its ``printer_model``) each name a catalogue row, and the
    two rows differ.  No model set up, no report, a report or a model the
    catalogue has no row for, and a disagreement its owner has settled for
    this machine (:func:`owner_statement`) are all ``None``.  Never raises.
    """
    try:
        found = _moves_disagreement(adapter)
        if found is None or found[1] is not None:
            return None
        check = found[0]
        name = getattr(adapter, "_kiln_registered_name", None)
        name = name if isinstance(name, str) else ""
        return (
            f"Nothing was sent to {name or 'the printer'}.  It is set up as {check.declared_name}, but it "
            f"reports itself as {check.reported_name}.  The moves Kiln sends are planned for the model a "
            "printer is set up as, and another printer's moves can drive the head into the bed or the "
            "frame, so until this is settled use the printer's own screen.  " + settle(check, name)
        )
    except Exception:  # noqa: BLE001 -- a comparison that cannot run proves nothing
        logger.debug("move model check failed", exc_info=True)
        return None


def statement_line(adapter: Any, printer_name: str = "") -> str | None:
    """The setup check's line for a printer whose disagreement its owner has settled, or ``None``."""
    try:
        found = _moves_disagreement(adapter)
    except Exception:  # noqa: BLE001 -- a line is help, never a fault
        return None
    if found is None or found[1] is None:
        return None
    check, said = found
    name = printer_name or "this printer"
    when = str(said.get("said_at") or "")[:10] or "an earlier day"
    how = "at a terminal" if said.get("how") == "user_terminal" else "with a code shown on this computer's screen"
    return (
        f"set up as {check.declared_name} and reports itself as {check.reported_name}: its owner said on "
        f"{when}, {how}, that {statement_text(said, name)}, so Kiln plans its moves for "
        f"{check.declared_name}.  Setting its model to what it reports takes that back."
    )


__all__ = [
    "MISMATCH_CODE",
    "STATEMENT_FIELD",
    "ModelCheck",
    "check_printer_model",
    "entry_statement",
    "maker_upgrades",
    "move_refusal",
    "owner_statement",
    "reported_row",
    "settle",
    "stated_upgrade",
    "statement_covers",
    "statement_line",
    "statement_text",
    "upgrade_between",
]
