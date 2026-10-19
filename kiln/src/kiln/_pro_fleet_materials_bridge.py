"""Public-Kiln → Kiln's servers: a material answer across several machines.

On an install with one machine every material-inventory answer is worked out
in :mod:`kiln.material_inventory`, offline.  The same question across several
machines -- the totals over all of them, which printer holds a material,
which printer takes each job, which spool moves where, when each material
runs out -- is Kiln Business (https://kiln3d.com/pricing) and is answered by
kiln-pro from the rows this install sends: its spools, what each printer has
loaded, its completed prints, the jobs asked about.  Nothing else is sent
and nothing is kept on Kiln's servers.

Asked in one of two places, in order:

1. **kiln-pro on this computer** (imported by name in :func:`_local`, never
   by a string, so a static reader of the call graph sees the crossing).
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

Below Business the router refuses before this module is reached.  A miss
(offline, signed out, no answer, refused) is worded here in the shared
served-answer voice and changes nothing on this computer.  Nothing here
raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, fields, local_fault, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers a material question across machines.
WIRE_TOOL = "plan_fleet_materials"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.fleet_materials"


def fleet_answer(
    question: str,
    *,
    rows: dict[str, Any],
    params: dict[str, Any],
    feature: str,
) -> tuple[Any, dict[str, Any] | None]:
    """``(answer, None)`` from kiln-pro, or ``(None, refusal)`` worded for
    the door to return as it is."""
    local = _local(question, rows, params)
    if local is not None:
        answer, miss = local
    else:
        answer, miss = _served(question, rows, params)
    if miss is None:
        return answer, None
    return None, _miss_refusal(feature, miss)


def _miss_refusal(feature: str, miss: Miss) -> dict[str, Any]:
    text = sentence(
        miss,
        feature="servers",
        on_the_line=f"{feature} answers across your machines on Kiln's servers",
        cannot="get that answer",
        wont="has no cross-machine answer for you",
        safe_remedy="Ask about one printer for its own answer",
        then="ask again",
    )
    return {
        "success": False,
        "error": text,
        "code": "FLEET_ANSWER_UNAVAILABLE",
        "retryable": miss.cause != "refused",
        **fields(miss),
    }


#: The one question answered with a single record; every other is a list.
_RECORD_QUESTIONS = frozenset({"forecast"})


def _read(question: str, answer: Any) -> tuple[Any, Miss | None]:
    if (
        isinstance(answer, dict)
        and answer.get("success") is True
        and answer.get("status") == "ok"
        and answer.get("question") == question
        and "answer" in answer
    ):
        value = answer["answer"]
        shape = dict if question in _RECORD_QUESTIONS else list
        if isinstance(value, shape) and (shape is dict or all(isinstance(v, (dict, str)) for v in value)):
            return value, None
        return None, Miss("unanswered", detail="an answer in a shape this install does not read")
    return None, classify_answer(answer) or Miss("unanswered", detail="an answer with no material answer in it")


def _local(question: str, rows: dict[str, Any], params: dict[str, Any]) -> tuple[Any, Miss | None] | None:
    """kiln-pro's answer on this computer, or ``None`` when it is not here."""
    try:
        from kiln_pro.fleet_materials import answer as local_answer
    except ImportError:
        return None
    try:
        return _read(question, local_answer(question, params=params, **rows))
    except Exception as exc:  # noqa: BLE001 -- a fault here is a miss to word, never a traceback
        return None, local_fault(exc)


def _served(question: str, rows: dict[str, Any], params: dict[str, Any]) -> tuple[Any, Miss | None]:
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", "NOT_ASKED", "this install could not open its connection to Kiln's servers")
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            # The person asked this question of their machines.
            _asked_by_user=True,
            question=question,
            params=params,
            **rows,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("fleet material answer not served", exc_info=True)
        return None, classify_transport_error(exc)
    return _read(question, answer)


__all__ = ["LOCAL_MODULE", "WIRE_TOOL", "fleet_answer"]
