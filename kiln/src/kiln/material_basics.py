"""What a material pick says when it has no pick: the plastics a printer can
melt, read from the nozzle temperature table Kiln ships with, and one line
saying why there is no pick and what fixes it.

A door with no answer from Kiln's servers names these instead of picking
one: a list a person can choose from, with no ranking and no judgement
about the part beyond the food-contact floor.
"""

from __future__ import annotations

from typing import Any

from kiln.gcode import _MATERIAL_TEMPS

__all__ = ["basic_pick_line", "basic_plastics", "plastics_printer_can_melt", "pro_pick_missed_line"]


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


def basic_plastics(use_text: Any, max_hotend_temp_c: float | None = None) -> list[str]:
    """The plastics a basic answer lists: those the hotend can melt, kept to
    the ones the food-contact floor allows when the part touches food."""
    from kiln.material_safety import filter_food_safe_materials, use_case_implies_food_contact

    plastics = plastics_printer_can_melt(max_hotend_temp_c)
    if use_case_implies_food_contact(use_text if isinstance(use_text, str) else ""):
        plastics = filter_food_safe_materials(plastics, require="yes_or_conditional")
    return plastics


def basic_pick_line(miss: Any, *, feature: str, hotend_known: bool = True) -> str:
    """The one line a material pick says when no pick came: Kiln did not pick
    a plastic for this part, why (:mod:`kiln.served_answer`), and what fixes
    it.  *hotend_known* says whether the list was narrowed to the printer."""
    from kiln.served_answer import Miss, sentence

    if miss is None:
        miss = Miss("unanswered", detail="no pick")
    return sentence(
        miss,
        feature=feature,
        on_the_line="Kiln did not pick a plastic for this part",
        cannot="pick one",
        wont="lists the plastics this printer can melt instead" if hotend_known else "lists common plastics instead",
        then=(
            "Kiln picks the plastic for this part"
            if miss.cause == "signed_out"
            else "ask again for Kiln's pick for this part"
        ),
    )


def pro_pick_missed_line(miss: Any, *, feature: str) -> str:
    """The one line said beside the pick every plan gets when the caller's
    plan includes Kiln Pro and Kiln Pro's own pick did not come: why
    (:mod:`kiln.served_answer`) and what fixes it, never an offer of the plan
    the caller holds."""
    from kiln.served_answer import Miss, sentence

    if miss is None:
        miss = Miss("unanswered", detail="no Kiln Pro pick")
    return sentence(
        miss,
        feature=feature,
        on_the_line="This is the pick every plan gets, not Kiln Pro's pick",
        cannot="give Kiln Pro's pick",
        wont="gives the pick every plan gets instead",
        then="ask again for Kiln Pro's pick",
    )
