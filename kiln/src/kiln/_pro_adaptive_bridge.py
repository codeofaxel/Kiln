"""Public-Kiln -> kiln-pro bridge for one thing: Kiln's adaptive slicing
settings tuned to a material, and the layer plan built from them.

The tuned settings are kiln-pro's (https://kiln3d.com), free at every plan.
With kiln-pro installed here (Kiln's servers) the answer is read in this
process (:func:`_local`, which imports kiln-pro by name); on a plain install
it is asked of Kiln's servers as :data:`WIRE_TOOL`, through the door every
served tool uses (:func:`kiln.server._pro_api_call`).  Nothing is kept on
this computer.

:func:`ask` answers ``(answer, None)`` when Kiln answered -- the settings,
the plan or the list of materials, or Kiln's own refusal of a value it
cannot use -- and ``(None, miss)`` when it did not: this computer offline,
Kiln signed out, the servers silent or saying no.  On a miss the adaptive
slicing tools plan with :func:`kiln.adaptive_slicer.generic_profile` and
say so in one line (:func:`basic`); a generic answer never reads as a tuned
one.  Nothing here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "adaptive_slicing_answer"
#: What the sign-in sentence calls the answer asked for.
SIGN_IN_NAME = "Kiln's tuned slicing settings"
#: Where the same answer is read when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.adaptive_slicing.engine"
#: A person is waiting on the answer; past this, generic settings answer.
CONSULT_TIMEOUT_S: float = 15.0

#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()
#: The key each question's answer carries.
_ANSWER_KEY = {"plan": "plan", "quick_plan": "plan", "profile": "profile", "materials": "materials"}

#: The key the one line rides under, the same on every basic answer.
NOTE_KEY = "message"

#: Per door: what is on the line, what Kiln can't do, what it did instead,
#: and what the fix leads to.  ``{m}`` is the material.  Nothing promises
#: tuned settings: Kiln may not tune a material it was asked about.
_WORDS: dict[str, tuple[str, str, str, str]] = {
    "plan": (
        "This plan uses generic settings worked out from the nozzle size, not settings tuned to {m}",
        "check whether it has settings tuned to {m}",
        "planned the layers with the generic settings",
        "ask again",
    ),
    "profile": (
        "These are generic settings worked out from the nozzle size, not settings tuned to {m}",
        "check whether it has settings tuned to {m}",
        "gave the generic settings",
        "ask again",
    ),
    "materials": (
        "Kiln's servers list the materials Kiln tunes",
        "get that list",
        "lists none, and every material plans with generic settings",
        "ask again",
    ),
}

__all__ = ["CONSULT_TIMEOUT_S", "LOCAL_MODULE", "NOTE_KEY", "WIRE_TOOL", "ask", "basic"]


def ask(question: str, **fields: Any) -> tuple[dict[str, Any] | None, Miss | None]:
    """Kiln's answer to *question* (``plan``, ``quick_plan``, ``profile`` or
    ``materials``) with *fields*, or why there is none.  Never raises."""
    local = _local(question, fields)
    if isinstance(local, Miss):
        return None, local
    if local is not _NOT_HERE:
        return local, None
    return _served(question, fields)


def basic(miss: Miss, material: Any = "", door: str = "plan") -> dict[str, Any]:
    """The fields a generic answer carries: ``basic`` and the one line that
    says what it is, why, and what gets the tuned answer."""
    from kiln import served_answer

    name = str(material or "").strip().upper() or "this material"
    on_the_line, cannot, wont, then = (part.format(m=name) for part in _WORDS[door])
    text = served_answer.sentence(
        miss, feature="servers", on_the_line=on_the_line, cannot=cannot, wont=wont,
        then=then + served_answer.free_account_note(miss),
    )
    out: dict[str, Any] = {"basic": True, NOTE_KEY: text, "retryable": miss.no_answer}
    out.update(served_answer.fields(miss))
    return out


def _local(question: str, fields: dict[str, Any]) -> Any:
    try:
        from kiln_pro.adaptive_slicing.engine import answer
    except ImportError:
        return _NOT_HERE
    try:
        out = answer(question, **fields)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        from kiln.served_answer import local_fault

        return local_fault(exc, where="Kiln's adaptive slicing settings")
    if _is_answer(out, question):
        return out
    return Miss("unanswered", detail="no adaptive slicing answer")


def _served(question: str, fields: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    from kiln import served_answer

    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door on this install is a miss, never a traceback
        return None, served_answer.local_fault(exc, where="the served door on this computer")
    try:
        # The person's own ask: these doors run only when someone asks for an
        # adaptive plan or a material's settings, which is what this fetches.
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=CONSULT_TIMEOUT_S,
            _asked_by_user=True,
            _feature=SIGN_IN_NAME,
            ask=question,
            **fields,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        host = getattr(_srv, "_HOSTED_KILN_API_URL", None)
        return None, served_answer.classify_transport_error(exc, host=host)
    if _is_answer(answer, question):
        return answer, None
    miss = served_answer.classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no adaptive slicing answer")


def _is_answer(answer: Any, question: str) -> bool:
    """An answer of Kiln's to *question*: one carrying what was asked for
    (the plan, the settings or the list), or Kiln's own refusal of a value
    it cannot use (relayed as it is)."""
    if not isinstance(answer, dict):
        return False
    if answer.get("success") is True and answer.get("status") == "ok":
        key = _ANSWER_KEY.get(question)
        return key is not None and key in answer
    return answer.get("success") is False and answer.get("code") == "INVALID_INPUT" and bool(answer.get("error"))
