"""What a material pick says when it has no pick: the plastics a printer can
melt, read from the nozzle temperature table Kiln ships with, and one line
saying why there is no pick and what fixes it.

A door with no answer from Kiln's servers names these instead of picking
one: a list a person can choose from, with no ranking and no judgement
about the part beyond two floors: the food-contact floor, and the plastics
Kiln's public rules for what the part must do leave out
(:func:`requirement_floor`), said in one line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kiln.gcode import _MATERIAL_TEMPS

logger = logging.getLogger(__name__)

__all__ = [
    "BasicList",
    "basic_list",
    "basic_pick_line",
    "basic_plastics",
    "pick_then",
    "plastics_printer_can_melt",
    "pro_pick_missed_line",
    "requirement_floor",
]


def plastics_printer_can_melt(max_hotend_temp_c: float | None = None) -> list[str]:
    """The plastics in the nozzle temperature table whose lowest printing
    temperature the hotend reaches, in the table's order.

    With no hotend ceiling, or one that is not a number (too large to be
    one included), every plastic in the table.
    """
    names = list(_MATERIAL_TEMPS)
    if max_hotend_temp_c is None:
        return names
    try:
        ceiling = float(max_hotend_temp_c)
    except (TypeError, ValueError, OverflowError):
        return names
    if ceiling != ceiling:  # NaN
        return names
    return [name for name in names if _MATERIAL_TEMPS[name][0] <= ceiling]


@dataclass(frozen=True)
class BasicList:
    """What a basic answer lists, and the line it says about the list.

    ``plastics`` are unranked, in the nozzle table's order.  ``rules_line``
    names the plastics Kiln's public rules for this part left out (and any
    still listed that they allow only with conditions), or is ``""`` when
    they left none out."""

    plastics: list[str]
    rules_line: str = ""


def basic_list(use_text: Any, max_hotend_temp_c: float | None = None) -> BasicList:
    """The plastics a basic answer lists: those the hotend can melt, kept to
    the ones the food-contact floor allows when the part touches food and to
    the ones Kiln's public rules for the part do not leave out
    (:func:`requirement_floor`), with the line saying what those rules did."""
    from kiln.material_safety import filter_food_safe_materials, use_case_implies_food_contact

    text = use_text if isinstance(use_text, str) else ""
    plastics = plastics_printer_can_melt(max_hotend_temp_c)
    if use_case_implies_food_contact(text):
        plastics = filter_food_safe_materials(plastics, require="yes_or_conditional")
    kept, line = requirement_floor(text, plastics)
    return BasicList(kept, line)


def basic_plastics(use_text: Any, max_hotend_temp_c: float | None = None) -> list[str]:
    """The plastics :func:`basic_list` lists, without its line."""
    return basic_list(use_text, max_hotend_temp_c).plastics


def requirement_floor(use_text: Any, names: list[str]) -> tuple[list[str], str]:
    """*names* without the plastics Kiln's public rules for what this part
    must do leave out, in their given order, and one line saying so.

    The rules are the public requirement profiles the part's description
    matches (:func:`kiln.design_intelligence.match_requirements`): a hot car
    part, a load, a liquid, food.  A plastic one of them excludes is left
    out, and the line names the rules and what they left out, with any
    plastic still listed that a matched rule allows only with conditions.
    This is a floor, never a ranking: nothing is put first and nothing is
    called best.  When the rules leave out every name, the line says what
    the rules name for the part instead.  ``(names, "")`` when no rule
    leaves out any of them, or the rules cannot be read.
    """
    from kiln.catalog_keys import resolve_material_key

    text = use_text if isinstance(use_text, str) else ""
    listed = list(names)
    if not text.strip() or not listed:
        return listed, ""
    try:
        from kiln.design_intelligence import match_requirements

        rules = match_requirements(text)
    except Exception:  # noqa: BLE001 -- unreadable rules leave the list as it was
        logger.debug("requirement rules unreadable", exc_info=True)
        return listed, ""

    def _keys(rule: Any, field: str) -> list[str]:
        value = (getattr(rule, "constraint_rules", None) or {}).get(field)
        return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []

    left_out: list[str] = []
    named_by: list[str] = []
    excluded_keys: set[str] = set()
    for rule in rules:
        exclude = _keys(rule, "material_exclude")
        excluded_keys.update(exclude)
        hit = [n for n in listed if resolve_material_key(n, exclude) is not None]
        if hit:
            left_out.extend(n for n in hit if n not in left_out)
            named_by.append(rule.display_name)
    if not left_out:
        return listed, ""
    kept = [n for n in listed if n not in left_out]
    conditional: list[str] = []
    for rule in rules:
        hit = [n for n in kept if resolve_material_key(n, _keys(rule, "material_conditional")) is not None]
        if hit:
            conditional.extend(n for n in hit if n not in conditional)
            if rule.display_name not in named_by:
                named_by.append(rule.display_name)
    lead = f"Kiln's rules for {_and(named_by)} parts"
    if not kept:
        wanted = []
        for rule in rules:
            for key in (*_keys(rule, "material_require"), *_keys(rule, "material_prefer")):
                if key not in excluded_keys:
                    name = _plastic_name(key)
                    if name not in wanted:
                        wanted.append(name)
        line = f"{lead} leave out every plastic listed here ({_and(left_out)})"
        line += f"; they name {_and(wanted)} for it." if wanted else "."
        return kept, line
    line = f"{lead} leave out {_and(left_out)}"
    if conditional:
        line += f", and allow {_and(conditional)} only with conditions"
    return kept, f"{line}."


def _plastic_name(key: str) -> str:
    """How a rule's material key is said: as the nozzle temperature table
    spells it when the table has it, else the key written as a name."""
    from kiln.catalog_keys import resolve_material_key

    for name in _MATERIAL_TEMPS:
        if resolve_material_key(name, [key]) is not None:
            return name
    return key.upper().replace("_", "-")


def _and(names: list[str]) -> str:
    """``"PLA"``, ``"PLA and TPU"``, ``"PLA, TPU and Nylon"``."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


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
        then=pick_then(miss),
    )


def pick_then(miss: Any) -> str:
    """What the fix to a missed material pick leads to, for the line that
    says why: Kiln's pick for this part, and for someone who never signed in,
    that a free account is enough."""
    from kiln.served_answer import free_account_note

    if getattr(miss, "cause", "") == "signed_out":
        return "Kiln picks the plastic for this part" + free_account_note(miss)
    return "ask again for Kiln's pick for this part"


#: What a line about Kiln Pro's pick says first, whatever came of it.
_NOT_KILN_PROS = "This is the pick every plan gets, not Kiln Pro's pick"


def pro_pick_missed_line(miss: Any, *, feature: str) -> str:
    """The one line said beside the pick every plan gets when the caller's
    plan includes Kiln Pro and Kiln Pro's own pick did not come: why
    (:mod:`kiln.served_answer`) and what fixes it, never an offer of the plan
    the caller holds.  *feature* names Kiln Pro's pick, the thing that
    missed.  *miss* ``None`` is Kiln Pro's pick answering with nothing for
    the part, which is not a miss and has nothing to fix."""
    from kiln.served_answer import sentence

    if miss is None:
        return f"{_NOT_KILN_PROS}. Kiln Pro's pick had nothing for this part."
    return sentence(
        miss,
        feature=feature,
        on_the_line=_NOT_KILN_PROS,
        cannot="give Kiln Pro's pick",
        wont="gives the pick every plan gets instead",
        then="ask again for Kiln Pro's pick",
    )
