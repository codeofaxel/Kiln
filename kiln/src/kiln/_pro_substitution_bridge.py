"""Public Kiln -> Kiln's servers: which filaments can stand in for another.

``find_material_substitute`` and ``get_best_material_substitute`` list the
filaments that can replace one material, ranked, with what each swap trades
away.  That ranking is Kiln's and is answered by its servers
(https://kiln3d.com), free on every plan for any signed-in account; nothing
sent is kept.  Two ways in, in order:

1. **kiln-pro on this computer** (Kiln's hosted servers): asked in process of
   the same function that answers the wire, imported by name in
   :func:`_local`, never by a string, so a static reader of the call graph
   sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

A miss (offline, signed out, no answer, refused) is never a ranking.  The
doors answer with the basic list instead (:func:`basic_substitutes`): the
other filaments the bundled catalogue files under the same category and names
after the same base polymer, alphabetical, with each one's catalogue name and
category and nothing else -- no order of merit, no match, no trade-off, no
price.  One line (:func:`basic_line`, in :mod:`kiln.served_answer`'s voice)
says what the list is, why Kiln's ranking is missing and how to get it.
Nothing here raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kiln.plain_words import listed as _listed
from kiln.served_answer import (
    Miss,
    ask_briefly,
    classify_answer,
    fields,
    free_account_note,
    local_fault,
    read_refusal,
    sentence,
)

logger = logging.getLogger(__name__)

#: The served tool that answers one material's substitutes.
WIRE_TOOL = "material_substitutes"
#: What the sign-in sentence calls the answer asked for.
SIGN_IN_NAME = "Kiln's substitute ranking"
#: Where the same answer is worked out when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_substitution"
#: The reasons a substitute can be asked to address: the doors' input words.
REASONS = ("unavailable", "cost", "lead_time", "strength", "finish_quality", "heat_resistance")
#: The longest filament name the doors take, and the most of anything a
#: caller sent that a refusal repeats; Kiln's servers refuse the same.
MAX_MATERIAL_CHARS = 64


@dataclass(frozen=True)
class Asked:
    """What came back for one ask: ``substitutes`` (Kiln's ranked list, best
    first) when Kiln answered; ``refusal`` (``code`` and ``error``) when its
    servers could not read the request; ``miss`` when no answer came."""

    substitutes: list[dict[str, Any]] | None = None
    refusal: dict[str, str] | None = None
    miss: Miss | None = None


def refusal_for(material: Any, *, reason: Any = None, min_score: Any = 0.5, best: bool = False) -> str | None:
    """Why the doors refuse this request before asking, or ``None``.

    A material that is not text or is longer than :data:`MAX_MATERIAL_CHARS`;
    a reason that is not one of :data:`REASONS`; a *min_score* that is not a
    number.  Any number is a threshold (one below every match lists them
    all, one above every match lists none).  With *best*, only the material
    is read.  A refusal repeats at most :data:`MAX_MATERIAL_CHARS`
    characters of what was sent.
    """
    if not isinstance(material, str):
        return "Invalid material: name the filament as text, e.g. 'PLA'."
    if len(material) > MAX_MATERIAL_CHARS:
        return f"Invalid material: a filament name is at most {MAX_MATERIAL_CHARS} characters."
    if best:
        return None
    if reason:
        if not isinstance(reason, str):
            return f"Invalid reason: name it as text. Valid: {_listed(list(REASONS))}"
        if reason.lower() not in REASONS:
            return f"Invalid reason: {reason[:MAX_MATERIAL_CHARS]!r}. Valid: {_listed(list(REASONS))}"
    if not isinstance(min_score, (int, float)):
        return "Invalid min_score: give a number; matches run from 0.0 to 1.0."
    return None


def ask(material: str, *, reason: str | None = None, min_score: float = 0.5, best: bool = False) -> Asked:
    """Kiln's substitutes for *material*: every one scoring at least
    *min_score* (only those addressing *reason*, when given), best first; or
    with *best*, the single best one at any score.  Never raises."""
    local = _local(material, reason, min_score, best)
    if local is not _NOT_HERE:
        return local
    return _served(material, reason, min_score, best)


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(material: str, reason: str | None, min_score: float, best: bool) -> Any:
    """kiln-pro's answer, asked here exactly as the wire asks it."""
    try:
        from kiln_pro.material_substitution import substitutes_for_the_wire
    except ImportError:
        return _NOT_HERE
    try:
        answer = substitutes_for_the_wire(material, reason=reason, min_score=min_score, best=best)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return Asked(miss=local_fault(exc, where="Kiln's substitution ranking"))
    return _read(answer)


def _served(material: str, reason: str | None, min_score: float, best: bool) -> Asked:
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001
        return Asked(miss=Miss("unanswered", detail=str(exc)[:120]))
    sent: dict[str, Any] = {"material": material}
    if best:
        sent["best"] = True
    else:
        sent.update(reason=reason, min_score=min_score)
    answer, miss = ask_briefly(
        # The person asked for substitutes, and this is the answer they
        # asked for: a signed-out refusal is them meeting the sign-in wall.
        lambda wait: _srv._pro_api_call(WIRE_TOOL, _timeout=wait, _asked_by_user=True, _feature=SIGN_IN_NAME, **sent),
        host=getattr(_srv, "_HOSTED_KILN_API_URL", None),
    )
    if miss is not None:
        return Asked(miss=miss)
    return _read(answer)


def _read(answer: Any) -> Asked:
    if isinstance(answer, dict):
        if answer.get("success") is True and isinstance(answer.get("substitutes"), list):
            return Asked(substitutes=[dict(s) for s in answer["substitutes"] if isinstance(s, dict)])
        # The servers' own ruling on a request they could not read.
        refused = read_refusal(answer)
        if answer.get("success") is False and answer.get("code") == "VALIDATION_ERROR" and refused.given:
            return Asked(refusal={
                "code": "VALIDATION_ERROR", "error": refused.words("Kiln's servers could not read that request."),
            })
    return Asked(miss=classify_answer(answer) or Miss("unanswered", detail="no substitutes in the answer"))


# ---------------------------------------------------------------------------
# The basic answer, for a miss
# ---------------------------------------------------------------------------


def _catalogue_entry(material: Any) -> tuple[str, dict[str, Any]] | None:
    """The bundled catalogue's id for *material* and the catalogue, or
    ``None`` when the catalogue does not know the name."""
    from kiln.catalog_keys import resolve_material_key
    from kiln.design_intelligence import list_public_material_profiles

    try:
        profiles = {p.material_id: p for p in list_public_material_profiles()}
    except Exception:  # noqa: BLE001 -- no catalogue, no family
        logger.debug("the bundled material catalogue could not be read", exc_info=True)
        return None
    key = resolve_material_key(material if isinstance(material, str) else "", profiles)
    return (key, profiles) if key is not None else None


def catalogue_name(material: Any) -> str | None:
    """The bundled catalogue's name for *material*, or ``None`` when the
    catalogue does not know it."""
    entry = _catalogue_entry(material)
    return entry[1][entry[0]].display_name if entry is not None else None


def basic_substitutes(material: str) -> list[dict[str, str]]:
    """The other filaments of *material*'s family in the bundled catalogue.

    A family is the filaments the catalogue files under the same category
    whose catalogue ids are named after the same base polymer (``silk_pla``
    and ``pla_plus`` after ``pla``; a blend such as ``pc_abs`` is named after
    two and belongs to neither).  Another spelling of the same material is
    not a substitute for it and is left out, and a member the catalogue
    spells two ways is listed once.  Alphabetical by id, each with
    its catalogue name and category, and a filled filament also with the
    nozzle-wear caution every filled filament carries (``abrasive_caution``).
    Empty when the catalogue does not know the material or names no other
    filament of its family.
    """
    from kiln.catalog_keys import same_material_spelling
    from kiln.material_safety import abrasive_nozzle_floor

    entry = _catalogue_entry(material)
    if entry is None:
        return []
    key, profiles = entry
    base = _base_polymer(key, profiles)
    if base is None:
        return []
    category = profiles[key].category
    family: list[dict[str, str]] = []
    for other in sorted(profiles):
        if other == key or same_material_spelling(other, key):
            continue
        if profiles[other].category != category or _base_polymer(other, profiles) != base:
            continue
        if any(same_material_spelling(other, kept["substitute_material"]) for kept in family):
            continue  # one material under a second spelling is listed once
        name = profiles[other].display_name
        member = {"substitute_material": other, "display_name": name, "category": category}
        caution = abrasive_nozzle_floor(name) or abrasive_nozzle_floor(other)
        if caution:
            member["abrasive_caution"] = caution
        family.append(member)
    return family


def _base_polymer(key: str, catalogue: Any) -> str | None:
    """The catalogue material *key* is named after: the one word of its id,
    short names written out, that is itself a catalogue id; ``None`` when no
    word is, or several are."""
    from kiln.catalog_keys import _material_tokens

    named = {word for word in _material_tokens(key) if word in catalogue}
    return next(iter(named)) if len(named) == 1 else None


def basic_line(miss: Miss | None, material: str, *, found: bool, reason: str | None = None) -> str:
    """The one line a basic answer carries: what the list is, why Kiln's
    ranking is missing, and how to get it.  The material is named once, by
    the bundled catalogue's own name, or as "this material" when the
    catalogue does not know it; what the caller typed is never repeated.
    The door is asked about a material, never a part, so the list is said
    to be checked against none."""
    if miss is None:
        miss = Miss("unanswered", detail="no substitutes")
    label = catalogue_name(material) or "this material"
    then = "ask again for Kiln's ranked substitutes and their trade-offs" + free_account_note(miss)
    if not found:
        return sentence(
            miss,
            feature="servers",
            on_the_line=f"Kiln's catalogue lists no other filament of the same family as {label}",
            cannot="rank substitutes for it",
            wont="names none",
            then=then,
        )
    unchecked = "not ranked and not checked against any part"
    if reason:
        unchecked = f"not ranked, not chosen for {reason.lower().replace('_', ' ')} and not checked against any part"
    return sentence(
        miss,
        feature="servers",
        on_the_line=f"These are filaments of the same family as {label} in Kiln's catalogue, {unchecked}",
        cannot="rank them",
        wont="lists only the same family",
        safe_remedy="check each one against what the part has to do",
        then=then,
    )


def miss_fields(miss: Miss | None) -> dict[str, Any]:
    """The machine-readable twin of :func:`basic_line`, with ``retryable``:
    whether asking again later may get Kiln's ranking."""
    if miss is None:
        return {}
    return {**fields(miss), "retryable": miss.no_answer}


__all__ = [
    "LOCAL_MODULE",
    "MAX_MATERIAL_CHARS",
    "REASONS",
    "WIRE_TOOL",
    "Asked",
    "ask",
    "basic_line",
    "basic_substitutes",
    "catalogue_name",
    "miss_fields",
    "refusal_for",
]
