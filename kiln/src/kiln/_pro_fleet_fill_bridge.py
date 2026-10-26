"""Many copies of one part across this install's printers, asked of Kiln's servers.

Kiln Business shares copies of a part over every printer that can take them,
wherever there is safe room on each plate as it stands
(https://kiln3d.com/pricing).  The plan is worked out on Kiln's servers; this
side reads its own printers and plates (the same readings the fleet route
sends) and relays the answer.  Nothing sent is kept there.

A miss is said in the shared voice (:mod:`kiln.served_answer`) and nothing is
planned or queued: copies are never spread across printers on a guess.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The served plan (kiln-pro): the readings go in, the share comes back.
TOOL = "plan_fleet_fill"


def ask(
    file_path: str,
    copies: int,
    *,
    material: str,
    candidates: list[dict[str, Any]],
    adapters: dict[str, Any],
    quality_priority: int = 3,
    speed_priority: int = 3,
    version_id: str | None = None,
) -> dict[str, Any]:
    """The plan for *copies* of the part in *file_path* over *candidates*:
    ``{"success": True, "fill": <fleet_fill/1>}``, the server's own refusal
    as it wrote it, or this door's refusal for a miss.  Never raises."""
    import kiln.server as _srv
    from kiln import _pro_placement_bridge as bridge
    from kiln import served_answer

    job = {**bridge.job_envelope(file_path), "copies": int(copies)}
    names = [str(c["printer_id"]) for c in candidates]
    try:
        readings = bridge.fleet_readings(job, names, adapters, wire=True)
        extra = {"version_id": version_id} if version_id else {}
        answer = _srv._pro_api_call(
            TOOL,
            material=material,
            copies=int(copies),
            candidates=candidates,
            plates=readings,
            quality_priority=quality_priority,
            speed_priority=speed_priority,
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", TOOL, exc_info=True)
        return _miss(served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)))
    if isinstance(answer, dict) and (
        (answer.get("success") is True and isinstance(answer.get("fill"), dict))
        or (isinstance(answer.get("error"), dict) and answer["error"].get("code"))
        or answer.get("required_tier")
    ):
        return answer
    miss = served_answer.classify_answer(answer)
    return _miss(miss or served_answer.Miss("unanswered", detail="no plan in the answer"))


def _miss(miss: Any) -> dict[str, Any]:
    """The refusal when the plan could not be had: what Kiln could not do,
    why, and that nothing was planned or queued."""
    import kiln.server as _srv
    from kiln import served_answer

    text = served_answer.sentence(
        miss,
        feature="fleet planning",
        on_the_line="The plan says how many copies each of your printers takes",
        cannot="ask Kiln's servers how to share the copies out",
        wont="won't plan or queue any copies",
        safe_remedy="print copies on one printer with multi_copy_print",
    )
    out = _srv._error_dict(text, code="FLEET_FILL_UNAVAILABLE", retryable=miss.cause != "refused")
    out.update(served_answer.fields(miss))
    return out
