"""The printers set up on this computer, as a status read lists them.

One row per printer: its name, its model when one is on record, and which
one Kiln is working with.  Read from this computer's own records; no
printer is contacted to build it.  A row never carries anything that
locates, identifies or unlocks a printer, and a printer whose saved name
holds such a value is listed without its name.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["known_printers"]


def _model_of(name: str, adapter: Any) -> str:
    """The model on record for *name*: the saved config's, else the one its
    adapter was built with.  Never a question to the printer."""
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for

        model = str(resolve_printer_model_for(name) or "").strip()
        if model:
            return model
    except Exception:  # noqa: BLE001 -- a model is a label beside the name
        logger.debug("roster: no saved model for %r", name, exc_info=True)
    try:
        return str(adapter.declared_printer_model() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _name_gives_the_machine_away(name: str, model: str) -> bool:
    """Whether *name* holds the printer's own address, serial or codes, or
    anything else that reads as a place or a secret."""
    from kiln.printer_names import _saved_entry, identifiers_of, name_advice

    return name_advice(name, printer_model=model or None, identifiers=identifiers_of(_saved_entry(name))) is not None


def known_printers(registry: Any) -> list[dict[str, Any]]:
    """One row per printer in *registry*, sorted by name.

    ``name`` and, when known, ``printer_model``; ``kiln_is_working_with`` is
    present (``True``) on the one printer Kiln is working with.  A printer
    whose name gives the machine away has ``name_withheld`` in place of
    ``name``.  Raises what the registry raises: the caller decides whether a
    list is worth a reading.
    """
    from kiln.printers.engagement import current, machine_id

    adapters = registry.list_all()
    engaged = current()
    rows: list[dict[str, Any]] = []
    for name in registry.list_machines():
        adapter = adapters.get(name)
        model = _model_of(name, adapter)
        row: dict[str, Any] = {}
        try:
            withheld = _name_gives_the_machine_away(name, model)
        except Exception:  # noqa: BLE001 -- unsure whether a name is clean: it stays here
            logger.debug("roster: could not read the name %r", name, exc_info=True)
            withheld = True
        if withheld:
            row["name_withheld"] = True
        else:
            row["name"] = name
        if model:
            row["printer_model"] = model
        if engaged is not None and adapter is not None and engaged.machine and engaged.machine == machine_id(adapter):
            row["kiln_is_working_with"] = True
        rows.append(row)
    return rows
