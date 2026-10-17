"""The printers of the computer a call came from, when it is not this one.

A tool Kiln's servers run for an install (:mod:`kiln.served_public`) is
often about the caller's printer: which bed a part must fit, which nozzle a
wall is judged for.  The servers have no printer.  The caller's install
reads what each of its printers IS -- a name, a model, the nozzle the
printer reports for itself -- and sends it with the call
(:mod:`kiln.served_steps`, ``printer_facts``); for the length of that call
the servers hold it here (kiln-pro, https://kiln3d.com, sets it), and the
readers that would otherwise ask this computer's own printers ask this
instead: the printer's model (:mod:`kiln.printer_model_resolver`), the bed
(:mod:`kiln.stage_plate`) and the nozzle (:mod:`kiln.assumed_nozzle`).

Everywhere else nothing is held, and every reader asks this computer's own
printers as it always has.  The facts are the caller's word about their own
machine, judged as the same values typed into a tool's arguments would be;
never an address, a serial number or a credential.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

_facts: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "kiln_callers_printers", default=None,
)

#: Names that name no printer in particular: the caller's default is meant.
UNNAMED = frozenset({"", "default", "active"})


@contextlib.contextmanager
def serving(facts: Any) -> Iterator[None]:
    """Hold *facts* (a ``printer_facts`` reading: ``{"printers": [...]}``)
    for the block.  Anything else holds nothing."""
    held = facts if isinstance(facts, dict) and isinstance(facts.get("printers"), list) else None
    token = _facts.set(held)
    try:
        yield
    finally:
        _facts.reset(token)


def known() -> bool:
    """Whether this call carries the caller's printers."""
    return _facts.get() is not None


def printers() -> list[dict[str, Any]]:
    """Each printer the caller's install listed, or ``[]``."""
    held = _facts.get() or {}
    return [p for p in held.get("printers") or [] if isinstance(p, dict)]


def machines() -> list[str]:
    """Each of the caller's machines once, by the name listed first (the
    install marks a second name for one machine ``same_machine_as``)."""
    return [
        str(p.get("name") or "")
        for p in printers()
        if str(p.get("name") or "") and not p.get("same_machine_as")
    ]


def printer(name: str | None) -> dict[str, Any] | None:
    """The caller's printer *name* means: by its name, else by its model,
    else for an unnamed one the caller's default (or their only printer).
    ``None`` when it names none of them."""
    listed = printers()
    asked = str(name or "").strip()
    if asked.lower() not in UNNAMED:
        for key in ("name", "model"):
            chosen = [p for p in listed if str(p.get(key) or "") == asked]
            if len(chosen) == 1:
                return chosen[0]
        return None
    chosen = [p for p in listed if p.get("is_default")]
    if len(chosen) == 1:
        return chosen[0]
    return listed[0] if len(listed) == 1 else None


def model_of(name: str | None) -> str | None:
    """The model of the caller's printer *name* means, or ``None``."""
    found = printer(name)
    model = str((found or {}).get("model") or "").strip()
    return model or None


def nozzle_setting_mm(name: str | None) -> float | None:
    """The nozzle size the caller's printer *name* reports for itself, as
    their install read it, or ``None``."""
    value = (printer(name) or {}).get("nozzle_setting_mm")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


__all__ = ["UNNAMED", "known", "machines", "model_of", "nozzle_setting_mm", "printer", "printers", "serving"]
