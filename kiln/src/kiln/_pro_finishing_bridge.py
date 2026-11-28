"""Public Kiln -> Kiln's servers: how to finish a print in one plastic.

``get_post_processing_guide`` answers how to sand, smooth, paint or
strengthen a print in one plastic, and ``get_print_diagnostic`` adds that
plastic's strengthening tips.  The finishing guide is Kiln's
(https://kiln3d.com) and is answered by its servers, free on every plan for
any signed-in account; nothing sent is kept.  Two ways in, in order:

1. **kiln-pro on this computer** (Kiln's servers): asked in process of the
   same function that answers the wire, imported by name in :func:`_local`,
   never by a string, so a static reader of the call graph sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

A miss (offline, signed out, no answer, refused) is never a finishing answer,
and never silent about safety.  The guide answers basic instead
(:func:`basic_guide`): each finish the plastic takes, by name, and each way
of strengthening it that carries a caution, every one with its safety notes,
read from the bundled cautions table (:data:`CAUTIONS_FILE`), or only the
finish the caller named when it is one of them; for a plastic
the table does not list, one textbook step that suits any plastic with its
own safety note.  Either is marked ``basic`` and carries one line
(:func:`basic_line`, in :mod:`kiln.served_answer`'s voice) saying why the
rest of the guide is missing and how to get it.  The diagnostic gives no
strengthening tip and says why in one line (:func:`tips_line`).  No line
repeats a material or goal the caller sent, and nothing here raises.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kiln.served_answer import (
    Miss,
    classify_answer,
    classify_transport_error,
    fields,
    local_fault,
    read_refusal,
    sentence,
)

logger = logging.getLogger(__name__)

#: The served tool that answers the finishing guide and the strengthening tips.
WIRE_TOOL = "finishing_guide"
#: Where the same answer is worked out when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.finishing"
#: What a person reads about a miss, filled in by :mod:`kiln.served_answer`.
FEATURE = "finishing guide"
#: The longest material name or goal the doors take; Kiln's servers refuse
#: the same.
MAX_TEXT_CHARS = 64
#: The codes of the servers' own answers that are the door's answer rather
#: than a miss: no finishing record for the material, a goal the record holds
#: nothing for, a request they could not read.  Each carries its sentence.
DOOR_CODES = ("NO_FINISHING_DATA", "UNKNOWN_GOAL", "VALIDATION_ERROR")
#: The diagnostic asks for the tips on the way to its own answer, so the
#: served ask waits this long before the miss is said instead.
TIPS_TIMEOUT_S: float = 4.0

#: The guide's reply keys, in order, with no goal and with one.
OVERVIEW_KEYS = ("material", "available_goals", "techniques", "paintability", "strengthening")
GOAL_KEYS = ("material", "goal", "answer")
#: The reply keys of the general answer Kiln's servers give when the caller's
#: daily allowance of distinct plastics is used: each finish and caution of
#: the plastic with its safety notes, and the line saying why.
GENERAL_KEYS = ("material", "general", "techniques", "strengthening_cautions", "message")

#: Each plastic's finishes with their safety notes: what a miss still says.
CAUTIONS_FILE = Path(__file__).parent / "data" / "design_knowledge" / "finishing_cautions.json"

#: The goals the guide answers by name; any other goal names one finish.
GOAL_WORDS = frozenset(
    {
        "surface",
        "finish",
        "finishing",
        "surface_finish",
        "paint",
        "painting",
        "coat",
        "coating",
        "strength",
        "strengthen",
        "strengthening",
        "anneal",
        "annealing",
    }
)

#: What any print can be finished with, whatever it is made of: one textbook
#: step, and how not to get hurt doing it.  Nothing here is about one plastic.
BASIC_STEPS: tuple[dict[str, str], ...] = (
    {
        "name": "Sanding",
        "how": (
            "Start with a coarse grit to take the layer lines down and work up "
            "through finer grits to the finish you want."
        ),
        "safety_notes": "Wear a dust mask: plastic dust should not be breathed.",
    },
)


@dataclass(frozen=True)
class Asked:
    """What came back for one guide ask: ``answer`` (the guide's answer) when
    Kiln answered; ``error`` (the door's own sentence) when Kiln's servers
    answered that there is nothing to show; ``miss`` when no answer came."""

    answer: dict[str, Any] | None = None
    error: str | None = None
    miss: Miss | None = None


def refusal_for(material: Any, goal: Any = None) -> str | None:
    """Why the doors refuse this request before asking, or ``None``.

    A material that is not text or is longer than :data:`MAX_TEXT_CHARS`; a
    goal that is not text or is longer than that.  No refusal repeats what
    was sent.
    """
    if not isinstance(material, str):
        return "Invalid material: name the plastic as text, e.g. 'PLA'."
    if len(material) > MAX_TEXT_CHARS:
        return f"Invalid material: a material name is at most {MAX_TEXT_CHARS} characters."
    if goal is not None:
        if not isinstance(goal, str):
            return "Invalid goal: name it as text: surface_finish, paint, strengthen, or a technique's name."
        if len(goal) > MAX_TEXT_CHARS:
            return f"Invalid goal: a goal is at most {MAX_TEXT_CHARS} characters."
    return None


def guide(material: str, goal: str | None = None) -> Asked:
    """Kiln's finishing guide for *material*: the overview with no *goal*,
    else the answer for that goal.  Never raises."""
    local = _local(material, goal, False)
    if local is _NOT_HERE:
        local = _served(material, goal, False)
    return _read_guide(local)


def tips(material: str) -> tuple[list[str] | None, Miss | None]:
    """``(tips, None)`` -- the strengthening tips for *material*, each with
    the safety note of the technique it names, empty when none apply -- or
    ``(None, miss)`` when no answer came.  Never raises."""
    if refusal_for(material) is not None:
        # No plastic is named like this, so there is no tip to ask for.
        return [], None
    answer = _local(material, None, True)
    if answer is _NOT_HERE:
        answer = _served(material, None, True)
    if isinstance(answer, Miss):
        return None, answer
    if isinstance(answer, dict) and answer.get("success") is True:
        listed = answer.get("tips")
        if isinstance(listed, list) and all(isinstance(t, str) for t in listed):
            return list(listed), None
    return None, classify_answer(answer) or Miss("unanswered", detail="no strengthening tips in the answer")


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(material: str, goal: str | None, want_tips: bool) -> Any:
    """kiln-pro's answer, asked here exactly as the wire asks it."""
    try:
        from kiln_pro.finishing import finishing_for_the_wire
    except ImportError:
        return _NOT_HERE
    try:
        return finishing_for_the_wire(material, goal=goal, tips=want_tips)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return local_fault(exc, where="Kiln's finishing guide")


def _served(material: str, goal: str | None, want_tips: bool) -> Any:
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door here is a miss, never a traceback
        return local_fault(exc, where="the served door on this computer")
    sent: dict[str, Any] = {"material": material}
    if want_tips:
        sent["tips"] = True
    elif goal is not None:
        sent["goal"] = goal
    # The guide is what the person asked for, so a signed-out refusal is them
    # meeting the sign-in wall; the tips are asked on the way to a diagnostic,
    # which the person asked about the print, not about these.
    asked_by_user = not want_tips
    try:
        return _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=TIPS_TIMEOUT_S if want_tips else None,
            _asked_by_user=asked_by_user,
            **sent,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        return classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))


def _read_guide(answer: Any) -> Asked:
    if isinstance(answer, Miss):
        return Asked(miss=answer)
    if isinstance(answer, dict):
        if answer.get("success") is True and _is_guide(answer):
            return Asked(answer=answer)
        # The servers' own answer that there is nothing to show.
        refused = read_refusal(answer)
        if answer.get("success") is False and answer.get("code") in DOOR_CODES and refused.given:
            return Asked(error=refused.words("Kiln has no finishing guide to show for that."))
    return Asked(miss=classify_answer(answer) or Miss("unanswered", detail="no finishing guide in the answer"))


def _is_guide(answer: dict[str, Any]) -> bool:
    if not isinstance(answer.get("material"), str):
        return False
    if answer.get("general") is True:
        return (
            isinstance(answer.get("techniques"), list)
            and isinstance(answer.get("strengthening_cautions"), list)
            and isinstance(answer.get("message"), str)
        )
    if "goal" in answer:
        return isinstance(answer.get("goal"), str) and isinstance(answer.get("answer"), dict)
    return (
        isinstance(answer.get("available_goals"), list)
        and isinstance(answer.get("techniques"), list)
        and isinstance(answer.get("paintability"), dict)
        and isinstance(answer.get("strengthening"), list)
    )


def reply_from(answer: dict[str, Any]) -> dict[str, Any]:
    """The guide door's reply rebuilt from Kiln's answer, key for key, before
    its upgrade line is added (a general answer carries its own line)."""
    if answer.get("general") is True:
        keys = GENERAL_KEYS
    else:
        keys = GOAL_KEYS if "goal" in answer else OVERVIEW_KEYS
    return {"success": True, **{key: answer[key] for key in keys}}


# ---------------------------------------------------------------------------
# Technique names and the cautions table
# ---------------------------------------------------------------------------

#: A technique name may carry a product in brackets ("Epoxy Coating
#: (<product>)", "Dyeing with <product>") or lack a form word ("Flexible
#: Paint" for "Flexible Rubber Paint"), so a lookup matches on the name's
#: CORE: the bracketed tail, a "with ..." tail and the form word "rubber"
#: dropped.  Kiln's servers resolve a name by the same rule.
_TECHNIQUE_TAIL = re.compile(r"\s*\(.*\)\s*$|\s+with\s+.*$", re.IGNORECASE)
_TECHNIQUE_FORM_WORDS = re.compile(r"\brubber\b", re.IGNORECASE)


def technique_core(name: Any) -> str:
    """The comparable core of technique *name*: brackets, a ``with ...``
    tail and the form word dropped, whitespace folded, casefolded."""
    core = _TECHNIQUE_TAIL.sub("", str(name or "").strip())
    core = _TECHNIQUE_FORM_WORDS.sub(" ", core)
    return " ".join(core.split()).casefold()


def find_technique(techniques: list[Any], name: Any) -> dict[str, Any] | None:
    """The technique in *techniques* called *name* (any case, or a spelling
    with the same core).  An exact name wins; otherwise the one technique
    with that core, and ``None`` when two share it."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    rows = [t for t in techniques if isinstance(t, dict)]
    for technique in rows:
        if str(technique.get("name", "")).strip().casefold() == wanted:
            return technique
    core = technique_core(name)
    matches = [t for t in rows if technique_core(t.get("name", "")) == core]
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=1)
def _cautions_table() -> dict[str, Any]:
    try:
        raw = json.loads(CAUTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("finishing cautions table unreadable: %s", CAUTIONS_FILE, exc_info=True)
        return {}
    return {key: value for key, value in raw.items() if not key.startswith("_") and isinstance(value, dict)}


def strengthening_cautions(material: Any) -> list[dict[str, str]]:
    """Each way of strengthening *material* that carries a caution, each
    ``{"method", "safety_notes"}``, from the bundled cautions table; empty for
    a plastic it lists none for."""
    if not isinstance(material, str):
        return []
    record = _cautions_table().get(material.lower()) or {}
    return [
        {"method": str(s["method"]), "safety_notes": str(s["safety_notes"])}
        for s in record.get("strengthening") or []
        if isinstance(s, dict) and s.get("method") and str(s.get("safety_notes") or "").strip()
    ]


def cautions(material: Any) -> tuple[str, list[dict[str, str]]] | None:
    """``(material id, finishes)`` for *material* from the bundled cautions
    table: every finish the plastic takes, each ``{"name", "safety_notes"}``,
    in the table's order.  ``None`` for a plastic the table does not list.
    *material* is looked up as written, lower-cased, as Kiln's servers do."""
    if not isinstance(material, str):
        return None
    key = material.lower()
    record = _cautions_table().get(key)
    if record is None:
        return None
    finishes = [
        {"name": str(t["name"]), "safety_notes": str(t["safety_notes"])}
        for t in record.get("techniques") or []
        if isinstance(t, dict) and t.get("name") and str(t.get("safety_notes") or "").strip()
    ]
    return (key, finishes) if finishes else None


# ---------------------------------------------------------------------------
# A miss
# ---------------------------------------------------------------------------


def _label(material: Any) -> str:
    """The plastic as the bundled catalogue names it, or "this plastic":
    what the caller typed is never repeated."""
    from kiln._pro_substitution_bridge import catalogue_name

    return catalogue_name(material) or "this plastic"


def basic_line(miss: Miss | None, material: Any, *, finishes_named: bool = False) -> str:
    """The one line the basic guide carries: Kiln did not give the rest of
    the guide for this plastic, why, and what fixes it.  *finishes_named*
    when the answer names the plastic's own finishes with their safety
    notes, rather than the one general step."""
    if miss is None:
        miss = Miss("unanswered", detail="no finishing guide")
    label = _label(material)
    return sentence(
        miss,
        feature=FEATURE,
        on_the_line=f"Kiln did not give the finishing steps for {label}",
        cannot="give them",
        wont=(
            "names each finish with how not to get hurt doing it, and no steps, tools, paints or strength figures"
            if finishes_named
            else "lists one general step that suits any plastic instead"
        ),
        then=(
            f"Kiln gives the finishing steps for {label}"
            if miss.cause == "signed_out"
            else f"ask again for the finishing steps for {label}"
        ),
    )


def basic_guide(miss: Miss | None, material: Any, goal: Any = None) -> dict[str, Any]:
    """The guide's answer when Kiln's could not be had.

    For a plastic the cautions table lists: ``techniques``, each finish it
    takes with its safety notes, and ``strengthening_cautions``, each way of
    strengthening it that carries a caution with its safety notes (only the
    finish the caller named, when *goal* names one of them).  Otherwise
    ``steps``, the one general step.  Either way ``basic: True``, the line
    saying why and its machine-readable twin; never a tool, a paint or a
    strength figure."""
    miss = miss or Miss("unanswered", detail="no finishing guide")
    listed = cautions(material)
    if listed is None:
        return {
            "success": True,
            "basic": True,
            "steps": [dict(step) for step in BASIC_STEPS],
            "message": basic_line(miss, material),
            **fields(miss),
        }
    key, finishes = listed
    strengthening = strengthening_cautions(material)
    if isinstance(goal, str) and goal.strip().lower().replace("-", "_").replace(" ", "_") not in GOAL_WORDS:
        named = find_technique(finishes, goal)
        if named is not None:
            finishes, strengthening = [named], []
    return {
        "success": True,
        "basic": True,
        "material": key,
        "techniques": [dict(finish) for finish in finishes],
        "strengthening_cautions": strengthening,
        "message": basic_line(miss, material, finishes_named=True),
        **fields(miss),
    }


def tips_line(miss: Miss | None, material: Any) -> str:
    """The one line the diagnostic carries in place of strengthening tips it
    could not get: why, and what fixes it.  Never read as "no tip applies"."""
    if miss is None:
        miss = Miss("unanswered", detail="no strengthening tips")
    label = _label(material)
    return sentence(
        miss,
        feature=FEATURE,
        on_the_line=f"Kiln did not give the strengthening tips for {label}",
        cannot="give them",
        wont="gives none",
        then=(
            f"Kiln gives the strengthening tips for {label}"
            if miss.cause == "signed_out"
            else f"ask again for the strengthening tips for {label}"
        ),
    )


__all__ = [
    "BASIC_STEPS",
    "CAUTIONS_FILE",
    "DOOR_CODES",
    "FEATURE",
    "GENERAL_KEYS",
    "GOAL_KEYS",
    "GOAL_WORDS",
    "LOCAL_MODULE",
    "MAX_TEXT_CHARS",
    "OVERVIEW_KEYS",
    "TIPS_TIMEOUT_S",
    "WIRE_TOOL",
    "Asked",
    "basic_guide",
    "basic_line",
    "cautions",
    "find_technique",
    "guide",
    "refusal_for",
    "reply_from",
    "strengthening_cautions",
    "technique_core",
    "tips",
    "tips_line",
]
