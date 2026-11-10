"""How a Bambu names an AMS tray — one rule, one place."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "AMS_HT_UNIT_IDS",
    "CHAINED_UNIT_IDS",
    "EXTERNAL_SPOOL_TRAY",
    "NO_TRAY",
    "TRAYS_PER_UNIT",
    "TrayRef",
    "describe_tray_id",
    "feeding_record",
    "read_extruder_slot",
    "read_tray_id",
    "tray_id",
    "tray_name",
    "unit_name",
]

TRAYS_PER_UNIT = 4
#: ``tray_now`` / load ``target`` for the external spool holder.
EXTERNAL_SPOOL_TRAY = 254
#: ``tray_now`` for "nothing feeding"; the load ``target`` that unloads.
NO_TRAY = 255
CHAINED_UNIT_IDS = range(0, 16)
AMS_HT_UNIT_IDS = range(128, 136)

_LETTERS = "ABCDEFGHIJKLMNOP"
_VIRTUAL_UNITS = (EXTERNAL_SPOOL_TRAY, NO_TRAY)


def tray_id(unit: int, slot: int) -> int:
    """The printer's id for *slot* of *unit*."""
    unit = int(unit)
    slot = int(slot)
    if unit in AMS_HT_UNIT_IDS:
        if slot != 0:
            raise ValueError(f"an AMS HT (unit {unit}) has one slot, 0; slot {slot} does not exist")
        return unit
    if unit in CHAINED_UNIT_IDS and slot >= 0:
        return unit * TRAYS_PER_UNIT + slot
    raise ValueError(f"no Bambu AMS unit has id {unit} (chained units are 0-15, AMS HT 128-135)")


@dataclass(frozen=True)
class TrayRef:
    """A tray id read off the wire, resolved to what it names."""

    tray_id: int
    unit: int | None
    slot: int | None

    @property
    def external(self) -> bool:
        return self.tray_id == EXTERNAL_SPOOL_TRAY

    @property
    def none(self) -> bool:
        return self.tray_id == NO_TRAY

    @property
    def loaded_tray(self) -> bool:
        """A tray on a unit — something ``ams_mapping`` and the load command can name."""
        return self.unit is not None

    @property
    def name(self) -> str:
        """The vendor's own name: ``B2``, ``HT-A``, ``Ext``; empty for "no tray"."""
        if self.unit is not None and self.slot is not None:
            return tray_name(self.unit, self.slot)
        return "Ext" if self.external else ""


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        value = int(text)
    return value if isinstance(value, int) else None


def read_tray_id(value: Any) -> TrayRef | None:
    """Resolve a ``tray_now`` / ``target`` / ``ams_mapping`` value; ``None`` when it is not one."""
    value = _as_int(value)
    if value is None:
        return None
    if value in (EXTERNAL_SPOOL_TRAY, NO_TRAY):
        return TrayRef(value, None, None)
    if value in AMS_HT_UNIT_IDS:
        return TrayRef(value, value, 0)
    if 0 <= value < len(CHAINED_UNIT_IDS) * TRAYS_PER_UNIT:
        unit, slot = divmod(value, TRAYS_PER_UNIT)
        return TrayRef(value, unit, slot)
    return None


def read_extruder_slot(value: Any) -> TrayRef | None:
    """Resolve a per-nozzle ``snow`` / ``star`` / ``spre`` value; ``None`` when it is not one."""
    value = _as_int(value)
    if value is None or value < 0 or value > 0xFFFF:
        return None
    unit, slot = value >> 8, value & 0xFF
    if slot == 0xFF:
        return TrayRef(NO_TRAY, None, None)
    if unit in _VIRTUAL_UNITS:
        return TrayRef(EXTERNAL_SPOOL_TRAY, None, None)
    try:
        return TrayRef(tray_id(unit, slot), unit, slot)
    except ValueError:
        return None


def feeding_record(ref: TrayRef | None, source: str) -> dict[str, Any] | None:
    """The ``feeding`` entry an AMS report carries: ``None`` when nothing feeds."""
    if ref is None or ref.none:
        return None
    return {"tray_id": ref.tray_id, "unit": ref.unit, "slot": ref.slot, "name": ref.name, "source": source}


def unit_name(unit: int) -> str:
    """``A``-``P`` for a chained unit, ``HT-A``-``HT-H`` for an AMS HT."""
    unit = int(unit)
    if unit in AMS_HT_UNIT_IDS:
        return "HT-" + _LETTERS[unit - AMS_HT_UNIT_IDS.start]
    if unit in CHAINED_UNIT_IDS:
        return _LETTERS[unit]
    raise ValueError(f"no Bambu AMS unit has id {unit}")


def tray_name(unit: int, slot: int) -> str:
    """The name the vendor shows for a tray: ``A1``…``D4``, or ``HT-A`` (one slot)."""
    tray_id(unit, slot)  # refuse an impossible pair the same way
    unit = int(unit)
    if unit in AMS_HT_UNIT_IDS:
        return unit_name(unit)
    return f"{unit_name(unit)}{int(slot) + 1}"


def describe_tray_id(value: Any) -> str:
    """A tray id for a sentence: ``tray 5 (slot B2)``, ``the external spool``, ``no tray``."""
    ref = read_tray_id(value)
    if ref is None:
        return f"tray {value!r} (not an id this printer uses)"
    if ref.external:
        return "the external spool"
    if ref.none:
        return "no tray"
    return f"tray {ref.tray_id} (slot {ref.name})"
