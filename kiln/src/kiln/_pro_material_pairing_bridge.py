"""Public-Kiln -> Kiln's servers: how materials pair in one print.

Two questions, both answered by kiln-pro (https://kiln3d.com):

* :func:`co_print` -- ``kiln.design_intelligence.check_multi_material_compatibility``
  asks whether two materials can share a dual-extrusion print: the verdict,
  the interface adhesion and the soluble-support record.
* :func:`mix_check` -- ``kiln.material_safety.check_material_compatibility``
  asks, for the materials one print loads, which pairs are risky or must
  never share it, what to purge between them and the hardware each needs.

Each is asked in one of two places, in order:

1. **kiln-pro on this computer** (Kiln's servers, and an install with
   kiln-pro), imported by name in :func:`_local_co_print` and
   :func:`_local_mix_check`, never by a string, so a static reader of the call
   graph sees the crossing.  There the curated notes and general rules come
   back with a pairing too.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL` and
   :data:`MIX_TOOL`, asked through the door every served tool uses
   (:func:`kiln.server._pro_api_call`).

A miss (offline, signed out, no answer, refused) comes back as a
:class:`kiln.served_answer.Miss` and never as a verdict: the caller answers
from what this computer can judge on its own and says why the rest is
missing.  Nothing is kept on either side, and nothing here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, local_fault

logger = logging.getLogger(__name__)

#: The served tool that answers one pair.
WIRE_TOOL = "material_pairing"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_pairing.engine"
#: The served tool that checks every pair of materials one print loads.
MIX_TOOL = "material_mix_check"
#: Where the same check is read from when kiln-pro IS installed here.
MIX_LOCAL_MODULE = "kiln_pro.material_pairing.compat"
#: What the sign-in sentence calls each answer asked for.
SIGN_IN_NAME = "Kiln's pairing check"
MIX_SIGN_IN_NAME = "Kiln's multi-material check"
#: Which record answered, as the answer names it.
FOUND = ("pair", "support", "none")
#: The keys a multi-material answer carries.
_ENTRY_KEYS = ("names", "pairs", "hardware")
#: The longest material name the pairing check reads.
MAX_MATERIAL_CHARS = 64

#: ``_local_co_print``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def name_problem(material_a: Any, material_b: Any) -> str | None:
    """Why the pairing check cannot read these two names, or ``None``.

    Each must be text of at most :data:`MAX_MATERIAL_CHARS` characters.  The
    sentence says what a name must be and repeats nothing that was sent.
    """
    if not isinstance(material_a, str) or not isinstance(material_b, str):
        return "Both materials must be given as names."
    if len(material_a) > MAX_MATERIAL_CHARS or len(material_b) > MAX_MATERIAL_CHARS:
        return f"A material name is at most {MAX_MATERIAL_CHARS} characters."
    return None


def co_print(material_a: str, material_b: str) -> tuple[dict[str, Any] | None, Miss | None]:
    """``(answer, None)`` for the pair, or ``(None, miss)`` when no answer
    came.  The answer carries ``found``, ``compatible``,
    ``interface_adhesion``, ``notes``, ``support_pair`` and
    ``general_rules``.  Names the check cannot read (:func:`name_problem`)
    are refused here, before anything is asked.  Never raises."""
    problem = name_problem(material_a, material_b)
    if problem is not None:
        return None, Miss("refused", code="INVALID_INPUT", detail=problem)
    local = _local_co_print(material_a, material_b)
    if local is not _NOT_HERE:
        return local  # type: ignore[return-value]
    return _served_co_print(material_a, material_b)


def _local_co_print(material_a: str, material_b: str) -> Any:
    try:
        from kiln_pro.material_pairing.engine import co_print_with_notes as _engine_co_print
    except ImportError:
        return _NOT_HERE
    try:
        answer = _engine_co_print(material_a, material_b)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return None, local_fault(exc, where="Kiln's pairing check")
    if _is_answer(answer):
        return answer, None
    return None, Miss("unanswered", detail="no pairing verdict in the answer")


def _served_co_print(material_a: str, material_b: str) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door here is a miss, never a traceback
        return None, local_fault(exc, where="the served door on this computer")
    try:
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            # The person asked whether these two materials print together;
            # the verdict is the feature they reached for.
            _asked_by_user=True,
            _feature=SIGN_IN_NAME,
            material_a=material_a,
            material_b=material_b,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        return None, classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
    if _is_answer(answer):
        return answer, None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no pairing verdict in the answer")


def mix_check(names: list[str]) -> tuple[dict[str, Any] | None, Miss | None]:
    """``(entries, None)`` for the distinct material names one print loads,
    or ``(None, miss)`` when none came.  The entries carry ``names`` (as
    judged), ``pairs`` (``[i, j, entry]`` for every pair ``i <= j``: a known
    pair's level, warning, mitigations and purge, or ``known: False`` with the
    default level and purge) and ``hardware`` (per name).  Never raises."""
    local = _local_mix_check(names)
    if local is not _NOT_HERE:
        return local  # type: ignore[return-value]
    return _served_mix_check(names)


def _local_mix_check(names: list[str]) -> Any:
    try:
        from kiln_pro.material_pairing.compat import pair_entries as _engine_entries
    except ImportError:
        return _NOT_HERE
    try:
        entries = _engine_entries(list(names))
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return None, local_fault(exc, where="Kiln's multi-material check")
    if _is_entries(entries):
        return entries, None
    return None, Miss("unanswered", detail="no multi-material check in the answer")


def _served_mix_check(names: list[str]) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door here is a miss, never a traceback
        return None, local_fault(exc, where="the served door on this computer")
    try:
        answer = _srv._pro_api_call(
            MIX_TOOL,
            # Asked on the way to a compose or a print; the person asked for
            # the plate, not for this check.
            _asked_by_user=False,
            _feature=MIX_SIGN_IN_NAME,
            materials=list(names),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        logger.debug("%s request failed", MIX_TOOL, exc_info=True)
        return None, classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
    if isinstance(answer, dict) and answer.get("success") is True and _is_entries(answer):
        return {key: answer[key] for key in _ENTRY_KEYS}, None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no multi-material check in the answer")


def _is_entries(entries: Any) -> bool:
    if not isinstance(entries, dict):
        return False
    names, pairs, hardware = entries.get("names"), entries.get("pairs"), entries.get("hardware")
    if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        return False
    if not (isinstance(hardware, list) and len(hardware) == len(names)
            and all(isinstance(h, dict) for h in hardware)):
        return False
    if not isinstance(pairs, list):
        return False
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) == 3 and isinstance(pair[2], dict)):
            return False
        i, j, entry = pair
        if not (isinstance(i, int) and isinstance(j, int) and 0 <= i < len(names) and 0 <= j < len(names)):
            return False
        if not (isinstance(entry.get("level"), str) and isinstance(entry.get("known"), bool)):
            return False
    return True


def _is_answer(answer: Any) -> bool:
    return (
        isinstance(answer, dict)
        and answer.get("success") is True
        and answer.get("found") in FOUND
        and isinstance(answer.get("material_a"), str)
        and isinstance(answer.get("material_b"), str)
        and isinstance(answer.get("compatible"), bool)
        and isinstance(answer.get("interface_adhesion"), str)
        and isinstance(answer.get("notes"), str)
        and (answer.get("support_pair") is None or isinstance(answer.get("support_pair"), dict))
        and isinstance(answer.get("general_rules"), list)
    )


__all__ = [
    "FOUND",
    "LOCAL_MODULE",
    "MAX_MATERIAL_CHARS",
    "MIX_LOCAL_MODULE",
    "MIX_TOOL",
    "WIRE_TOOL",
    "co_print",
    "mix_check",
    "name_problem",
]
