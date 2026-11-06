"""Whether Kiln can drive a printer, said the same way at every door.

Most catalogue printers are ones Kiln connects to and drives.  A few are not:
Kiln lists them, designs and slices for them, and has no connection to them
yet.  One or two are added before any has been tried on real hardware.  Which
is which lives in ``data/printer_support.json``, keyed by catalogue id, and
every door that sets a printer up or answers for it reads the sentence here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SUPPORT_PATH = Path(__file__).resolve().parent / "data" / "printer_support.json"

NOT_DRIVEN = "not_driven"
UNTESTED = "untested"
STATES = frozenset({NOT_DRIVEN, UNTESTED})

#: How a person tells Kiln what they found; said in both sentences.
REPORT_INVITE = "report_issue sends Kiln a note, and you choose what it says"


@dataclass(frozen=True)
class Support:
    """One printer's support, where it is not the ordinary case."""

    printer_id: str
    state: str
    start_with: str | None = None

    @property
    def driven(self) -> bool:
        return self.state != NOT_DRIVEN

    def sentence(self, name: str) -> str:
        """What a person is told about the printer called *name*."""
        if self.state == NOT_DRIVEN:
            how = f" with {self.start_with}" if self.start_with else " yourself"
            return (
                f"Kiln cannot drive the {name} yet: it has no connection to this printer. Kiln can "
                f"still design and slice for it, and you start the print{how}. If you have found a way "
                f"to reach yours on your own network, {REPORT_INVITE}."
            )
        return (
            f"Kiln supports the {name}, and this printer is new: it has not been tried on a real one "
            f"yet. Print something and tell us how it goes: {REPORT_INVITE}."
        )


@lru_cache(maxsize=1)
def _table() -> dict[str, Support]:
    try:
        data = json.loads(_SUPPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("printers") if isinstance(data, dict) else None
    table = {}
    for key, row in (rows or {}).items():
        if isinstance(row, Mapping) and row.get("state") in STATES:
            start = str(row["start_with"]).strip() if row.get("start_with") else None
            table[str(key)] = Support(str(key), str(row["state"]), start)
    return table


def support_for(printer_id: str | None) -> Support | None:
    """The row for the printer *printer_id* names, in any spelling the
    catalogue knows, or ``None`` for a printer Kiln drives in the ordinary way
    (and for one it does not know)."""
    if not printer_id:
        return None
    table = _table()
    if not table:
        return None
    key = str(printer_id).strip().lower().replace("-", "_").replace(" ", "_")
    if key in table:
        return table[key]
    try:
        from kiln.printer_profile_ids import map_printer_hint_to_profile_id

        resolved = map_printer_hint_to_profile_id(str(printer_id))
    except Exception:  # noqa: BLE001 -- a name Kiln cannot resolve has no row
        return None
    return table.get(resolved) if resolved else None


def support_sentence(printer_id: str | None) -> str | None:
    """The sentence for *printer_id* under its catalogue name, or ``None``."""
    support = support_for(printer_id)
    if support is None:
        return None
    try:
        from kiln.slicer_profiles import roster_display_name

        name = roster_display_name(support.printer_id) or support.printer_id
    except Exception:  # noqa: BLE001 -- the id is still a name
        name = support.printer_id
    return support.sentence(name)


__all__ = ["NOT_DRIVEN", "REPORT_INVITE", "STATES", "UNTESTED", "Support", "support_for", "support_sentence"]
