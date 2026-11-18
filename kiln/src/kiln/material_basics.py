"""The plastics a printer can melt, read from the nozzle temperature table
Kiln ships with.

A door with no answer from Kiln's servers names these instead of picking
one: a list a person can choose from, with no ranking and no judgement
about the part.
"""

from __future__ import annotations

from kiln.gcode import _MATERIAL_TEMPS

__all__ = ["plastics_printer_can_melt"]


def plastics_printer_can_melt(max_hotend_temp_c: float | None = None) -> list[str]:
    """The plastics in the nozzle temperature table whose lowest printing
    temperature the hotend reaches, in the table's order.

    With no hotend ceiling, or one that is not a number, every plastic in
    the table.
    """
    names = list(_MATERIAL_TEMPS)
    if max_hotend_temp_c is None:
        return names
    try:
        ceiling = float(max_hotend_temp_c)
    except (TypeError, ValueError):
        return names
    return [name for name in names if _MATERIAL_TEMPS[name][0] <= ceiling]
