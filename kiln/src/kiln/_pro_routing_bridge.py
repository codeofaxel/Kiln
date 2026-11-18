"""Public-Kiln -> Kiln's servers: which material to print a part in, from what
it is for.

``kiln.material_routing`` builds the ``recommend_material`` and
``list_available_materials`` replies from Kiln's material recommender
(https://kiln3d.com): the pick for an intent, its runners-up, the sentence
saying why it wins, and the material list behind it.  This module is where
that answer is asked for.  Free on every plan; nothing is kept on either
side.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (Kiln's servers, and an install with
   kiln-pro), imported by name in :func:`_local`.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).  A
   request carries the intent, the printer's enclosure and heated bed, the
   budget and the names of the materials on hand; never where a spool is or
   how much is left.

Each call answers ``(answer, None)`` or ``(None, miss)``: a miss (offline,
signed out, no answer, refused, a fault on this computer) is a
:class:`kiln.served_answer.Miss` the door words, never an answer.  Nothing
here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

#: The served tool that answers the pick and the material list.
WIRE_TOOL = "intent_materials"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.intent_materials.engine"

#: What a person reads about a miss, filled in by :mod:`kiln.served_answer`.
FEATURE = "material recommendation"
#: What the sign-in sentence calls the answer asked for.
SIGN_IN_NAME = f"Kiln's {FEATURE}"

#: The longest description of what a part is for that is asked about.
MAX_INTENT_CHARS = 10_000
#: The most on-hand material names one ask carries, and how much of each.
MAX_NAMES = 500
MAX_NAME_CHARS = 120

#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()

__all__ = [
    "FEATURE",
    "LOCAL_MODULE",
    "MAX_INTENT_CHARS",
    "MAX_NAMES",
    "MAX_NAME_CHARS",
    "WIRE_TOOL",
    "catalogue",
    "pick",
    "refusal_for",
]


def refusal_for(intent: Any) -> str | None:
    """Why the door refuses this request before asking, or ``None``: a
    description of the part that is not text, or longer than
    :data:`MAX_INTENT_CHARS` characters."""
    if not isinstance(intent, str):
        return "Invalid intent: say what the part is for, as text."
    if len(intent) > MAX_INTENT_CHARS:
        return f"Invalid intent: say what the part is for in at most {MAX_INTENT_CHARS:,} characters."
    return None


def pick(
    intent: str,
    *,
    printer_capabilities: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    on_hand_materials: list[str] | None = None,
    inventory_recorded: bool = False,
) -> tuple[dict[str, Any] | None, Miss | None]:
    """The material pick for *intent*, or why there is none.

    *printer_capabilities* is ``{"has_enclosure", "has_heated_bed"}`` or
    ``None``; *on_hand_materials* names the materials the caller has, as their
    inventory spells them, or ``None`` for the whole list.  Never raises.
    """
    sent = _names_for_the_wire(on_hand_materials)
    request: dict[str, Any] = {
        "intent": str(intent or ""),
        "printer_capabilities": printer_capabilities,
        "budget_usd": budget_usd,
        "on_hand_materials": None if sent is None else sent[:MAX_NAMES],
        "inventory_recorded": bool(inventory_recorded),
    }
    answer, miss = _ask("pick", request)
    if answer is None or on_hand_materials is None or sent is None:
        return answer, miss
    # Which catalogue material each on-hand name is, keyed by the name as
    # this computer spells it.
    matched = answer.get("on_hand_catalog") or {}
    own = {name: matched[wire] for name, wire in zip(on_hand_materials, sent, strict=True) if wire in matched}
    return {**answer, "on_hand_catalog": own}, None


def catalogue() -> tuple[list[dict[str, Any]] | None, Miss | None]:
    """The material list, by name, or why there is none.  Never raises."""
    answer, miss = _ask("catalogue", {})
    if answer is None:
        return None, miss
    return [dict(m) for m in answer["materials"]], None


def _names_for_the_wire(names: list[str] | None) -> list[str] | None:
    """The on-hand material names as they leave this computer: read off its
    own records, so each passes the outbound guard, and cut to
    :data:`MAX_NAME_CHARS` characters (a material is named in its first
    words)."""
    if names is None:
        return None
    from kiln.outbound_guard import scrub

    return [str(scrub(str(name)))[:MAX_NAME_CHARS] for name in names]


def _ask(want: str, request: dict[str, Any]) -> tuple[Any, Miss | None]:
    local = _local(want, request)
    if isinstance(local, Miss):
        return None, local
    if local is not _NOT_HERE:
        return _checked(want, local)
    return _served(want, request)


def _local(want: str, request: dict[str, Any]) -> Any:
    try:
        from kiln_pro.intent_materials import engine
    except ImportError:
        return _NOT_HERE
    try:
        return engine.for_the_wire(want=want, **request)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        from kiln.served_answer import local_fault

        return local_fault(exc, where="Kiln's material recommender")


def _served(want: str, request: dict[str, Any]) -> tuple[Any, Miss | None]:
    held = _already_missed()
    if held is not None:
        return None, held
    try:
        import kiln.server as _srv
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", detail="the served door is not available")
    try:
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            # The person asked which material to use, or for the list; the
            # answer is what they reached for.
            _asked_by_user=True,
            _feature=SIGN_IN_NAME,
            want=want,
            **request,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        return None, classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
    return _checked(want, answer)


def _already_missed() -> Miss | None:
    """The miss of a call Kiln's servers were already asked for and did not
    answer (``kiln.served_public`` runs it here after that), so they are not
    asked a second time; ``None`` otherwise."""
    try:
        from kiln import served_public

        if served_public.already_missed():
            _answer, miss = served_public.answer_or_missed(WIRE_TOOL, {})
            return miss if isinstance(miss, Miss) else Miss("unanswered")
    except Exception:  # noqa: BLE001 -- unreadable: ask as usual
        logger.debug("served_public state unreadable", exc_info=True)
    return None


def _checked(want: str, answer: Any) -> tuple[Any, Miss | None]:
    """*answer* when it is the shape asked for, else the miss it amounts to."""
    if _is_answer(want, answer):
        return answer, None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no material recommendation in the answer")


def _is_answer(want: str, answer: Any) -> bool:
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok"):
        return False
    if want == "catalogue":
        materials = answer.get("materials")
        return isinstance(materials, list) and all(_is_record(m) for m in materials)
    pick = answer.get("pick")
    alternatives = answer.get("alternatives")
    catalog = answer.get("on_hand_catalog", {})
    status = answer.get("on_hand_status")
    better = answer.get("better_catalog_option")
    return (
        _is_record(pick)
        and _is_number(answer.get("score"))
        and isinstance(answer.get("settings"), dict)
        and isinstance(answer.get("sentence"), str)
        and isinstance(alternatives, list)
        and all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in alternatives)
        and status in (None, "on_hand", "no_inventory_recorded", "needs_purchase")
        and isinstance(catalog, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in catalog.items())
        # The pick is said to be on hand only when it is one of the shelf's.
        and (status != "on_hand" or pick["name"] in catalog.values())
        and (better is None or (
            _is_record(better)
            and isinstance(better.get("sentence"), str)
            and _is_number(better.get("score"))
            and _is_number(better.get("score_gap"))
        ))
    )


def _is_record(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and isinstance(value.get("display_name"), str)
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
