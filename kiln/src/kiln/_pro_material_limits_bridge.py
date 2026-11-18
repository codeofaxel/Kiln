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

One plastic and one scenario per ask.  A miss (offline, signed out, no
answer, refused) comes back as a :class:`kiln.served_answer.Miss` and never as
a figure: :mod:`kiln.material_limits` answers with the general figures and
says why.  Nothing is kept here, and nothing here raises.
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


def _local(material: str, scenario: dict[str, Any]) -> Any:
    try:
        from kiln_pro.material_limits.engine import for_the_wire as _engine_limits
    except ImportError:
        return _NOT_HERE
    try:
        answer = _engine_limits(material, None, **scenario)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return None, local_fault(exc, where="the design limits on this computer")
    if _is_answer(answer):
        return _kept(answer), None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    return None, miss or Miss("unanswered", detail="no design limits in the answer")


def _served(material: str, scenario: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    global _service_down_until, _service_down_miss
    if time.monotonic() < _service_down_until:
        return None, _service_down_miss or Miss("unanswered", "SERVER_UNREACHABLE")
    try:
        import kiln.server as _srv
    except Exception as exc:  # noqa: BLE001 -- no served door here is a miss, never a traceback
        return None, local_fault(exc, where="the served door on this computer")
    try:
        answer = _srv._pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # Asked on the way to a design check, a print check or a slice;
            # the person asked for the check, not for this.
            _asked_by_user=False,
            material=material,
            **{key: value for key, value in scenario.items() if value is not None},
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        miss = classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        return None, miss
    if _is_answer(answer):
        return _kept(answer), None
    miss = classify_answer(answer) if isinstance(answer, dict) else None
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    return None, miss or Miss("unanswered", detail="no design limits in the answer")


def _kept(answer: dict[str, Any]) -> dict[str, Any]:
    """Only what a check reads, so nothing else an answer carries travels on."""
    material_id = answer.get("material_id")
    return {
        "known": answer["known"],
        "material_id": material_id if isinstance(material_id, str) else None,
        "limits": {q: dict(answer["limits"][q]) for q in QUANTITIES if q in answer["limits"]},
    }


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


def _is_answer(answer: Any) -> bool:
    if not (isinstance(answer, dict) and answer.get("success") is True and isinstance(answer.get("known"), bool)):
        return False
    figures = answer.get("limits")
    return isinstance(figures, dict) and all(q in figures and _is_figure(figures[q]) for q in QUANTITIES)


__all__ = ["BASES", "LOCAL_MODULE", "QUANTITIES", "WIRE_TOOL", "available", "ask"]
