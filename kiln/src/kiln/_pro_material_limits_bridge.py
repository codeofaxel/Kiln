"""Public-Kiln -> Kiln's servers: one plastic's design limits.

How steep a face may lean, how far a bridge may span, how thin a wall and a
pin may go: the general figures are public (:mod:`kiln.generic_limits`), and
a plastic's own figures are kiln-pro's (https://kiln3d.com), asked in one of
two places, in order:

1. **kiln-pro on this computer** (Kiln's servers, and an install with
   kiln-pro), imported by name in :func:`_local`, never by a string, so a
   static reader of the call graph sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

One plastic and one scenario per ask.  :func:`ask_sliced` asks the same two
places what Kiln's own slice keeps at a nozzle (the thinnest wall and the
thinnest round pin), which is the same for every plastic and names none.  A
miss (offline, signed out, no answer, refused) comes back as a
:class:`kiln.served_answer.Miss` and never as a figure:
:mod:`kiln.material_limits` answers with the general figures and says why.
Nothing is kept here, and nothing here raises.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, local_fault

logger = logging.getLogger(__name__)

#: The served tool that answers one plastic's limits.
WIRE_TOOL = "material_limits"
#: What the sign-in sentence calls the answer asked for.
SIGN_IN_NAME = "Kiln's design-limit check"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.material_limits.engine"
#: The quantities an answer carries, each ``{value, unit, basis}``.
QUANTITIES: tuple[str, ...] = (
    "overhang_unsupported_deg",
    "bridge_max_mm",
    "wall_recommended_mm",
    "pin_min_mm",
    "esd_wall_min_mm",
)
#: What Kiln's own slice keeps at a nozzle, each ``{value, unit, basis}``:
#: the thinnest wall, across its layer, it lays a line in, and the thinnest
#: round pin it prints in every layer.
SLICED: tuple[str, ...] = ("sliced_wall_min_mm", "sliced_pin_min_mm")
#: Where a figure came from, as the answer says it.
BASES: tuple[str, ...] = ("plastic", "family", "general", "not_applicable")
#: A check waits this long, at most; the general figures are always there.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Miss | None = None

#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def available() -> bool:
    """True when kiln-pro is installed here with its design-limits reader."""
    try:
        import kiln_pro.material_limits.engine  # noqa: F401

        return True
    except ImportError:
        return False


def ask(
    material: str,
    *,
    nozzle_mm: float,
    layer_height_mm: float | None = None,
    enclosed: bool | None = None,
    pin_height_mm: float | None = None,
) -> tuple[dict[str, Any] | None, Miss | None]:
    """``(answer, None)`` for one plastic in this scenario, or ``(None, miss)``.

    The answer carries ``known``, ``material_id`` and ``limits``
    (``{quantity: {value, unit, basis}}`` for every quantity in
    :data:`QUANTITIES`).  Never raises."""
    scenario = {
        "nozzle_mm": float(nozzle_mm),
        "layer_height_mm": layer_height_mm,
        "enclosed": enclosed,
        "pin_height_mm": pin_height_mm,
    }
    local = _local(material, scenario)
    if local is not _NOT_HERE:
        return local  # type: ignore[return-value]
    return _served(material, scenario)


def ask_sliced(*, nozzle_mm: float) -> tuple[dict[str, Any] | None, Miss | None]:
    """``(answer, None)`` with what Kiln's own slice keeps at *nozzle_mm*, or
    ``(None, miss)``.

    The answer carries ``limits`` with every quantity in :data:`SLICED`.  No
    plastic is named: the figures are the same for every plastic.  Never
    raises."""
    scenario = {"nozzle_mm": float(nozzle_mm), "quantities": list(SLICED)}
    local = _local(None, scenario)
    if local is not _NOT_HERE:
        return local  # type: ignore[return-value]
    return _served(None, scenario)


def _wanted(scenario: dict[str, Any]) -> tuple[str, ...]:
    """The quantities an answer to *scenario* must carry: the ones it asked
    for, else every per-plastic quantity."""
    return tuple(scenario.get("quantities") or QUANTITIES)


def _local(material: str | None, scenario: dict[str, Any]) -> Any:
    try:
        from kiln_pro.material_limits.engine import for_the_wire as _engine_limits
    except ImportError:
        return _NOT_HERE
    asked = scenario.get("quantities")
    rest = {key: value for key, value in scenario.items() if key != "quantities"}
    try:
        answer = _engine_limits(material, list(asked) if asked else None, **rest)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return None, local_fault(exc, where="the design limits on this computer")
    if _is_answer(answer, _wanted(scenario)):
        return _kept(answer, _wanted(scenario)), None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no design limits in the answer")


def _served(material: str | None, scenario: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    global _service_down_until, _service_down_miss
    if time.monotonic() < _service_down_until:
        return None, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door here is a miss, never a traceback
        return None, local_fault(exc, where="the served door on this computer")
    named = {"material": material} if material is not None else {}
    try:
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # Asked on the way to a design check, a print check or a slice;
            # the person asked for the check, not for this.
            _asked_by_user=False,
            _feature=SIGN_IN_NAME,
            **named,
            **{key: value for key, value in scenario.items() if value is not None},
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        miss = classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if _is_answer(answer, _wanted(scenario)):
        return _kept(answer, _wanted(scenario)), None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return None, miss or Miss("unanswered", detail="no design limits in the answer")


#: The longest allowance line, or note on a figure, an answer may carry.
_ALLOWANCE_MAX_CHARS = 400


def _kept_figure(figure: dict[str, Any]) -> dict[str, Any]:
    """A figure's value, unit and basis, and the sentence saying why it is
    what it is where the answer gives one (``note``: a static-safe grade's
    minimum wall is a floor for its electrical property)."""
    kept = {key: figure[key] for key in ("value", "unit", "basis") if key in figure}
    note = figure.get("note")
    if isinstance(note, str) and note.strip() and len(note) <= _ALLOWANCE_MAX_CHARS:
        kept["note"] = note.strip()
    return kept


def _kept(answer: dict[str, Any], wanted: tuple[str, ...] = QUANTITIES) -> dict[str, Any]:
    """Only what a check reads, so nothing else an answer carries travels on.

    ``allowance`` is the line Kiln's servers send when they answered this
    plastic with the general figures because the caller's daily allowance
    of plastics was used; it is kept to be said, never read as a figure."""
    material_id = answer.get("material_id")
    kept: dict[str, Any] = {
        "known": answer["known"],
        "material_id": material_id if isinstance(material_id, str) else None,
        "limits": {q: _kept_figure(answer["limits"][q]) for q in wanted if q in answer["limits"]},
    }
    allowance = answer.get("allowance")
    if isinstance(allowance, str) and allowance.strip() and len(allowance) <= _ALLOWANCE_MAX_CHARS:
        kept["allowance"] = allowance.strip()
    return kept


def _is_figure(figure: Any) -> bool:
    if not isinstance(figure, dict) or figure.get("basis") not in BASES:
        return False
    value = figure.get("value")
    if value is None:
        return figure["basis"] == "not_applicable"
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _is_answer(answer: Any, wanted: tuple[str, ...] = QUANTITIES) -> bool:
    if not (isinstance(answer, dict) and answer.get("success") is True and isinstance(answer.get("known"), bool)):
        return False
    figures = answer.get("limits")
    if not isinstance(figures, dict) or not all(q in figures and _is_figure(figures[q]) for q in wanted):
        return False
    # What the slice keeps is always a figure, never "does not apply".
    return all(figures[q].get("value") is not None for q in wanted if q in SLICED)


__all__ = ["BASES", "LOCAL_MODULE", "QUANTITIES", "SLICED", "WIRE_TOOL", "available", "ask", "ask_sliced"]
