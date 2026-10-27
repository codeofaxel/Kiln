"""A printer's make and model, as the person declares it at setup.

Every door that sets a printer up (``kiln setup``, the bridge's setup,
``kiln auth``, ``kiln set-model``, the ``register_printer`` and
``set_printer_model`` tools) asks this module what a declared make and model
are, so one machine is never accepted by one door and refused by another.

A model Kiln's catalogue knows resolves to its catalogue key.  A model it
does not know is recorded honestly as ``<make>_<model>`` (``prusa_core_one_l``)
rather than refused or steered to a near name: the printer is held to the
generic safe limits until Kiln has its record, and the label is what tells
Kiln which printers people own.  The printer's own name (``"Sarah's
printer"``) is never part of it.

A model alone is enough when it names a catalogued printer.  A model the
catalogue does not know needs its make, which a connection type can supply
(a Bambu Lab, Creality, Elegoo or Prusa Link connection reaches only that
maker's printers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The model a printer's label may hold, in characters: the daily report
#: keeps labels of at most 48, so a longer one would never arrive.
MAX_LABEL_LEN = 48

#: Connection types that reach one maker's printers only, so a model typed
#: for one names that maker's printer.
_MAKE_OF_CONNECTION: dict[str, str] = {
    "bambu": "Bambu Lab",
    "creality": "Creality",
    "elegoo": "Elegoo",
    "prusa": "Prusa Research",
    "prusalink": "Prusa Research",
}


class PrinterDeclarationError(ValueError):
    """A declaration Kiln cannot record, with the code a door returns."""

    def __init__(self, code: str, message: str, close_matches: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.close_matches = close_matches or []


@dataclass(frozen=True)
class DeclaredPrinter:
    """What a declared make and model are."""

    #: What is saved as ``printer_model``: the person's own spelling when the
    #: catalogue knows it (so a Neptune 4 Plus stays one, whatever row its
    #: limits are read from), else the ``<make>_<model>`` label of a printer
    #: the catalogue does not know.
    model_key: str
    in_catalogue: bool
    #: The make as Kiln names it, or as typed for one it does not list.
    make: str | None
    close_matches: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        """One sentence a door adds when the model is not in the catalogue."""
        if self.in_catalogue:
            return ""
        close = (
            f"  If it is one of {', '.join(self.close_matches)}, name that instead."
            if self.close_matches else ""
        )
        return (
            f"Recorded as {self.model_key}.  Kiln has no record for this printer yet, so it is held to "
            f"generic safe limits and will not home or park its head until it has one.{close}"
        )


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def printer_makers() -> list[str]:
    """The makers whose printers are in Kiln's catalogue, alphabetically.

    Read from the catalogue's own printer names through the manufacturer
    rule design profiles use, so a printer added to the roster brings its
    maker with it.  A maker not listed is still accepted, typed in full.
    """
    from kiln.design_intelligence import _derive_manufacturer
    from kiln.printers.bed_fit import _load_printer_intelligence

    rows = _load_printer_intelligence() or {}
    makers = {
        _derive_manufacturer(key, str(row.get("display_name") or ""))
        for key, row in rows.items()
        if isinstance(row, dict) and not key.startswith("_") and key not in ("default", "klipper_generic")
    }
    makers.discard("Unknown")
    makers.discard("Generic")
    return sorted(makers, key=str.lower)


def _canonical_make(make: str | None, printer_type: str | None) -> str | None:
    typed = str(make or "").strip()
    if not typed:
        return _MAKE_OF_CONNECTION.get(str(printer_type or "").strip().lower())
    wanted = _slug(typed).replace("_", "")
    for known in printer_makers():
        compact = _slug(known).replace("_", "")
        if wanted in (compact, _slug(known.split()[0])):
            return known
    return typed


def _make_prefix(make: str) -> str:
    """The label prefix for *make*: the one its catalogue keys use when they
    share one (``Bambu Lab`` -> ``bambu``), else the make's first word."""
    from kiln.motion_facts import catalogue_keys

    keys = catalogue_keys()
    for candidate in (_slug(make).replace("_", ""), _slug(make.split()[0]) if make.split() else ""):
        if candidate and any(key.startswith(candidate) for key in keys):
            return candidate
    return _slug(make.split()[0]) if make.split() else _slug(make)


def _make_of_key(key: str) -> str | None:
    from kiln.design_intelligence import _derive_manufacturer
    from kiln.printers.bed_fit import _load_printer_intelligence

    row = (_load_printer_intelligence() or {}).get(key)
    display = str(row.get("display_name") or "") if isinstance(row, dict) else ""
    return _derive_manufacturer(key, display)


def _catalogued(make: str | None, model: str) -> tuple[str | None, str, list[str]]:
    """``(catalogue key, spelling that reached it, close keys)`` for *make*
    and *model*; the key is ``None`` when the catalogue has no row."""
    from kiln.catalog_keys import resolve_printer_key
    from kiln.motion_facts import catalogue_keys
    from kiln.printer_profile_ids import resolve_declared_model

    keys = catalogue_keys()
    key, close = resolve_declared_model(model)
    key = key or resolve_printer_key(model, keys)
    if key and (not make or _make_of_key(key) == make):
        return key, model, []
    if make:
        whole = f"{make} {model}"
        named = resolve_printer_key(whole, keys)
        if named:
            return named, whole, []
    # "A1" under make Prusa is not the Bambu Lab A1: a model is never moved
    # to another maker's printer.
    return None, model, close


def declare_printer(
    make: str | None,
    model: str | None,
    *,
    printer_type: str | None = None,
) -> DeclaredPrinter:
    """Resolve a declared make and model, or raise :class:`PrinterDeclarationError`.

    Codes: ``PRINTER_MODEL_REQUIRED`` (no model), ``PRINTER_MAKE_REQUIRED``
    (a model the catalogue does not know, with no make given or implied by
    *printer_type*), ``INVALID_PRINTER_MODEL`` (nothing usable in it).
    """
    typed_model = str(model or "").strip()
    if _slug(typed_model) in ("klipper", "moonraker", "klipper_generic", "octoprint", "generic"):
        # The firmware or the connection is not which printer it is.
        raise PrinterDeclarationError(
            "PRINTER_MODEL_REQUIRED",
            f"{typed_model!r} is how the printer is reached, not which printer it is.  Say its make and "
            "model (for example, Voron 2.4 or Prusa Core One L); a printer Kiln does not list yet is fine.",
        )
    if not typed_model:
        raise PrinterDeclarationError(
            "PRINTER_MODEL_REQUIRED",
            "Say which printer this is: its make and model (for example, Prusa Core One L).  "
            "Kiln uses it to check prints against the printer's own limits.",
        )
    canonical = _canonical_make(make, printer_type)
    key, spelling, close = _catalogued(canonical, typed_model)
    if key:
        return DeclaredPrinter(model_key=spelling, in_catalogue=True, make=_make_of_key(key))
    if not canonical:
        raise PrinterDeclarationError(
            "PRINTER_MAKE_REQUIRED",
            f"Kiln does not know {typed_model!r} yet.  Say who makes it (for example, Prusa or Anycubic) "
            "so it is recorded as that printer.",
            close_matches=close,
        )
    # A hint names only the declared maker's printers: a Flsun owner is not
    # told to name a Creality row instead.
    close = [k for k in close if _make_of_key(k) == canonical]
    prefix = _make_prefix(canonical)
    rest = _slug(typed_model)
    for lead in {_slug(canonical), _slug(canonical.split()[0]), prefix}:
        if lead and (rest == lead or rest.startswith(lead + "_")):
            rest = rest[len(lead):].strip("_")
            break
    if not rest:
        raise PrinterDeclarationError(
            "INVALID_PRINTER_MODEL",
            f"{typed_model!r} names a maker, not a printer.  Say which {canonical} printer it is.",
            close_matches=close,
        )
    label = f"{prefix}_{rest}"[:MAX_LABEL_LEN].rstrip("_")
    return DeclaredPrinter(model_key=label, in_catalogue=False, make=canonical, close_matches=close)


__all__ = [
    "MAX_LABEL_LEN",
    "DeclaredPrinter",
    "PrinterDeclarationError",
    "declare_printer",
    "printer_makers",
]
