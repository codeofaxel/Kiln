"""Public-Kiln -> kiln-pro maintenance-guide bridge.

The one place public Kiln asks kiln-pro for a maker's repair guide.  Every
door that walks a person through a repair -- the ``repair_guide`` tool,
``kiln repair``, the ``kiln doctor`` coverage line, the one sentence
``troubleshoot_printer`` adds when a fault code maps to a guide -- reaches
the table through here, so a guide is read in exactly one place and every
door says the same thing.

Why the guides are not in this repo.  A guide is the maker's own page read
closely and reproduced in the maker's ORDER (the power-off warning first,
then the steps, each with the maker's picture); the table of which pages
exist for which model, and which fault code starts which guide at which
step, is know-how Kiln has paid for.  Public Kiln keeps the mechanism
(this bridge, the door wiring, the step mode) and a tiny map of each
maker's public maintenance INDEX per model -- a link is a funnel, not a
leak -- so an install that cannot reach a guide still answers honestly.

With kiln-pro installed here the table is read locally.  Otherwise this asks
Kiln's servers (https://kiln3d.com) for the one guide and the one step the
door is about to show, through the door every served tool uses
(``kiln.server._pro_api_call``): the guide's header, every step's title and
the body of that step, the way the hosted ``repair_guide`` serves it.  The
code -> guide pointer belongs to the code's fault row, so it rides the fault
bridge's served reading.  A call that gets no answer is kept as the reason
(:func:`guide_unavailable`), worded in the shared voice
(:mod:`kiln.served_answer`) by the door, which then shows the maker's own
pages -- never "your maker publishes no guide", which would be a claim
about the maker made without asking.

Pictures are never fetched here or anywhere in public Kiln: a guide carries
the maker's URL and alt text, and the maker's server serves the picture at
answer time.  kiln-pro applies any tier rule on ITS side
(``guide_for_caller``); nothing in this file decides a tier, and today
nothing is paywalled.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "maintenance_guide"
#: A repair step is asked for by a person at the machine; a short wait.
CONSULT_TIMEOUT_S: float = 8.0

#: The wire's names for the maker's page -> the guide record's.
_RECORD_NAMES: dict[str, str] = {"maker_page_url": "source_url", "maker_page_title": "source_title"}
#: ``model -> why the last ask for it got no answer`` (a
#: :class:`kiln.served_answer.Miss`), cleared by an answer.
_last_miss: dict[str, Any] = {}
#: ``model -> the coverage the servers last gave for it``: every answer
#: carries it, so the door that falls back to coverage asks nothing twice.
_coverage: dict[str, dict[str, Any]] = {}


def available() -> bool:
    """True when kiln-pro is installed and its guide reader imports."""
    try:
        import kiln_pro.device_intelligence.maintenance_guides  # noqa: F401

        return True
    except ImportError:
        return False


def _reader() -> Any | None:
    try:
        from kiln_pro.device_intelligence import maintenance_guides
    except ImportError:
        return None
    return maintenance_guides


def find_guide(
    printer_id: str,
    *,
    topic: str = "",
    code: str = "",
    guide: str = "",
    step: int = 0,
    plan_only: bool = False,
) -> dict[str, Any] | None:
    """The guide for *printer_id* that *guide*, *code* or *topic* names, or ``None``.

    Returns ``{"slug", "guide", "start_at_step"}`` -- the guide THIS caller is
    entitled to (kiln-pro's ``guide_for_caller`` applies the rule, which
    today is "everyone") -- or ``{"choices": [...]}`` when a topic names
    more than one guide for the model (two machines behind one printer id,
    or two pages for one job); the person picks by slug.  ``None`` means: no
    guide for this model and topic/code, a guide the caller's tier does not
    unlock, or no answer from Kiln's servers (:func:`guide_unavailable` says
    which); a door then answers with the maker's public maintenance index if
    it knows one.

    *step* and *plan_only* say what the door is about to show, so a served
    guide carries that step's body and only the titles of the rest; a local
    read has the whole table in hand and ignores them.
    """
    reader = _reader()
    if not printer_id:
        return None
    if reader is None:
        return _served_find(printer_id, topic=topic, code=code, guide=guide, step=step, plan_only=plan_only)
    try:
        found = reader.find_guide(printer_id, topic=topic, code=code, guide=guide)
        if found is None:
            return None
        if isinstance(found, list):
            return {"choices": [
                {
                    "guide": slug,
                    "maker_page_title": g.get("source_title"),
                    # The maker's own product name for the page ("Centauri
                    # Carbon 2 Combo", "A1 mini / A1"); null where the page
                    # names no product.
                    "machine": g.get("machine"),
                    "section": reader.section_of(slug),
                    "applies_also_to": g.get("applies_also_to"),
                    "topic": g.get("topic"),
                }
                for slug, g in found
            ]}
        slug, _guide, start = found
        served = reader.guide_for_caller(slug)
    except Exception as exc:  # noqa: BLE001 -- a guide is never worth a crash
        logger.debug("kiln-pro guide lookup failed for %s/%s/%s: %s", printer_id, topic, code, exc)
        return None
    if not isinstance(served, dict):
        return None
    return {"slug": slug, "guide": served, "start_at_step": int(start)}


def guide_for_code(code: str, *, kind: str | None = None) -> tuple[str, int] | None:
    """``(guide slug, start_at_step)`` when a fault code maps to a guide, else ``None``."""
    reader = _reader()
    if not code:
        return None
    if reader is None:
        from kiln import _pro_fault_bridge

        return _pro_fault_bridge.served_guide_for_code(code, kind=kind)
    try:
        return reader.guide_for_code(code, namespace=kind)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kiln-pro code->guide lookup failed for %s: %s", code, exc)
        return None


def coverage(printer_id: str) -> dict[str, Any] | None:
    """``{"maker", "count", "topics", ...}`` for one model, or ``None`` when
    there is no answer (:func:`guide_unavailable` says why)."""
    reader = _reader()
    if not printer_id:
        return None
    if reader is None:
        held = _coverage.get(printer_id)
        if held is not None:
            return dict(held)
        _ask(printer_id)
        held = _coverage.get(printer_id)
        return dict(held) if held is not None else None
    try:
        out = reader.coverage(printer_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kiln-pro guide coverage failed for %s: %s", printer_id, exc)
        return None
    return dict(out) if isinstance(out, dict) else None


def guide_unavailable(printer_id: str) -> Any | None:
    """Why the last served ask for *printer_id* got no answer (a
    :class:`kiln.served_answer.Miss`), or ``None`` when it was answered --
    always ``None`` with kiln-pro installed here, where nothing can miss."""
    return _last_miss.get(printer_id) if printer_id else None


def unavailable_line(printer_id: str, *, maker: str | None = None) -> dict[str, Any] | None:
    """The sentence a door says when *printer_id*'s guide could not be asked
    for, with its machine-readable why, or ``None`` when nothing was missed.

    ``{"line", "why", "why_code", "why_detail"}``.  Says what Kiln could not
    do and that the maker's own pages stand in, never that the maker
    publishes no guide: nobody asked.
    """
    miss = guide_unavailable(printer_id)
    if miss is None:
        return None
    from kiln.served_answer import fields, sentence

    whose = f"{maker}'s" if maker else "the maker's"
    return {
        "line": sentence(
            miss,
            feature="servers",
            on_the_line=f"Kiln steps through {whose} own repair guide for this printer, one step and one picture at a time",
            cannot="get the guide",
            wont=f"points at {whose} own maintenance pages instead",
            safe_remedy=f"Follow {whose} pages",
            then="ask again",
        ),
        **fields(miss),
    }


def _served_find(
    printer_id: str, *, topic: str, code: str, guide: str, step: int, plan_only: bool,
) -> dict[str, Any] | None:
    answer = _ask(printer_id, topic=topic, code=code, guide=guide, step=step, plan_only=plan_only)
    if answer is None:
        return None
    choices = answer.get("choices")
    if isinstance(choices, list) and choices:
        return {"choices": [dict(c) for c in choices if isinstance(c, dict)]}
    found = answer.get("found")
    if not isinstance(found, dict) or not isinstance(found.get("guide"), dict) or not found.get("slug"):
        return None
    try:
        start = int(found.get("start_at_step") or 1)
    except (TypeError, ValueError):
        start = 1
    # The wire names the maker's page as the hosted door does; the reader's
    # own record spells it as the table does.
    guide = {_RECORD_NAMES.get(key, key): value for key, value in found["guide"].items()}
    return {"slug": str(found["slug"]), "guide": guide, "start_at_step": max(1, start)}


def _ask(printer_id: str, **question: Any) -> dict[str, Any] | None:
    """Ask Kiln's servers; the answer, or ``None`` with why in :data:`_last_miss`."""
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    payload = {"printer_id": printer_id, **{k: v for k, v in question.items() if v not in ("", None, False)}}
    try:
        import kiln.server as _srv

        answer = _srv._pro_api_call(WIRE_TOOL, _timeout=CONSULT_TIMEOUT_S, _asked_by_user=False, **payload)
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        _last_miss[printer_id] = classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
        return None
    if isinstance(answer, dict) and answer.get("success") is True and "found" in answer:
        _last_miss.pop(printer_id, None)
        cover = answer.get("coverage")
        if isinstance(cover, dict):
            _coverage[printer_id] = dict(cover)
        return answer
    _last_miss[printer_id] = classify_answer(answer) or Miss("unanswered", detail="no guide in the answer")
    return None


__all__ = [
    "CONSULT_TIMEOUT_S",
    "WIRE_TOOL",
    "available",
    "coverage",
    "find_guide",
    "guide_for_code",
    "guide_unavailable",
    "unavailable_line",
]
