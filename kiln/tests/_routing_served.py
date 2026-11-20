"""A stand-in for Kiln's material recommendation, for public tests.

Public tests cannot import kiln-pro.  The recommender's answer is stood in for
at the one served door (``kiln.server._pro_api_call``), with made-up values:
these tests pin what public Kiln does with an answer and with a miss, never
what the answer is.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

WIRE = "intent_materials"


def record(name: str, display_name: str | None = None) -> dict[str, Any]:
    """A pick's record as the stand-in serves it: names only."""
    return {"name": name, "display_name": display_name or name.upper()}


def served_pick(
    name: str = "petg",
    display_name: str | None = None,
    *,
    sentence: str | None = None,
    alternatives: list[str] = (),
    on_hand_status: str | None = None,
    on_hand_catalog: dict[str, str] | None = None,
    better: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One served pick, shaped like the wire's answer."""
    shown = display_name or name.upper()
    return {
        "success": True,
        "status": "ok",
        "fallback": False,
        "pick": record(name, shown),
        "score": 77.7,
        "estimated_cost_usd": 1.5,
        "settings": {"stand_in": True},
        "sentence": sentence or f"{shown} is the stand-in pick.",
        "alternatives": [
            {"material": alt.upper(), "name": alt, "score": 11.1, "settings": {"stand_in": True}}
            for alt in alternatives
        ],
        "on_hand_status": on_hand_status,
        "on_hand_catalog": dict(on_hand_catalog or {}),
        "better_catalog_option": better,
    }


def stand_in_answer(request: dict[str, Any]) -> dict[str, Any]:
    """A served answer that follows the request's shelf: the first of PETG or
    PLA on hand, else a catalogue pick labelled as the request warrants.  A
    stand-in for the door's own wiring, never the recommender."""
    names = request.get("on_hand_materials")
    if names is None:
        return served_pick("petg", "PETG")
    catalog = {n: n.split("-")[0].lower() for n in names if n.split("-")[0].lower() in ("petg", "pla")}
    if catalog:
        chosen = "petg" if "petg" in catalog.values() else "pla"
        return served_pick(chosen, chosen.upper(), on_hand_status="on_hand", on_hand_catalog=catalog)
    status = "needs_purchase" if request.get("inventory_recorded") else "no_inventory_recorded"
    return served_pick("petg", "PETG", on_hand_status=status)


@contextlib.contextmanager
def plain_install_served(answer: Any = None, *, answer_for=None, others=None):
    """A plain install whose served door answers the material recommendation
    with *answer* (or ``answer_for(request)``); a call to any other served tool
    is answered by *others* (a ``tool -> answer`` map) or as unreachable.

    Yields the list of ``(tool, request)`` pairs sent.
    """
    import kiln.server as srv
    from kiln import _pro_routing_bridge as bridge

    sent: list[tuple[str, dict[str, Any]]] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        sent.append((tool_name, dict(kwargs)))
        if tool_name == WIRE:
            if isinstance(answer, BaseException):
                raise answer
            return answer_for(kwargs) if answer_for is not None else answer
        if others and tool_name in others:
            return others[tool_name]
        return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE"}

    with patch.object(bridge, "_local", lambda want, request: bridge._NOT_HERE), \
            patch.object(srv, "_pro_api_call", fake):
        yield sent


SIGNED_OUT = {"status": "error", "success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out"}
