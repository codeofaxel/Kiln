"""Many copies of one part across this install's printers, asked of Kiln's servers.

Kiln Business shares copies of a part over every printer that can take them,
wherever there is safe room on each plate as it stands
(https://kiln3d.com/pricing).  The plan is worked out on Kiln's servers; this
side reads its own printers and plates (the same readings the fleet route
sends) and relays the answer.  Nothing sent is kept there.

A miss is said in the shared voice (:mod:`kiln.served_answer`) and nothing is
planned or queued: copies are never spread across printers on a guess.

The same wire answers for a plate of a run that FAILED (``failed=``): whether
its copies print again on the same printer, move to another the person's
yes covers, or stop, with the reasons in words.  On a miss the queue keeps
its own same-printer retry (:meth:`kiln.scheduler.JobScheduler.
_settle_fill_plate`) and says so: nothing moves to another printer on a
guess, and below Kiln Business nothing moves at all.
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
    times: dict[str, dict[str, float]] | None = None,
    share: str = "soonest",
    failed: dict[str, Any] | None = None,
    asked_by_user: bool = True,
) -> dict[str, Any]:
    """The plan for *copies* of the part in *file_path* over *candidates*,
    with each printer's slicer *times* for the part (the person's own
    readings) and the *share* they asked for:
    ``{"success": True, "fill": <fleet_fill/1>}``, the server's own refusal
    as it wrote it, or this door's refusal for a miss.  Never raises.

    With *failed* -- the queue's report of a plate of the run that failed
    (:func:`kiln.job_splitter.failed_plate_report`) -- *copies* are that
    plate's, and the answer also carries ``failed_plate`` (``failed_plate/1``):
    ``decision`` (``retry`` / ``move`` / ``stop``), the ``printer`` and
    ``why``.  *asked_by_user* says who asked: the plan is the person's own
    ask; a failed plate is the queue's, and then every reading in it goes
    through the outbound guard."""
    import kiln.server as _srv
    from kiln import _pro_placement_bridge as bridge
    from kiln import served_answer

    job = {**bridge.job_envelope(file_path), "copies": int(copies)}
    names = [str(c["printer_id"]) for c in candidates]
    try:
        readings = bridge.fleet_readings(job, names, adapters, wire=True)
        extra = {"version_id": version_id} if version_id else {}
        if failed is not None:
            extra["failed"] = failed
        answer = _srv._pro_api_call(
            TOOL,
            _asked_by_user=asked_by_user,  # the plan is the person's own ask; a failed plate is the queue's (asked_by_user=False)
            material=material,
            copies=int(copies),
            candidates=candidates,
            plates=readings,
            quality_priority=quality_priority,
            speed_priority=speed_priority,
            times=times or {},
            share=share,
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", TOOL, exc_info=True)
        return _miss(
            served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)),
            failed=failed is not None,
        )
    if isinstance(answer, dict) and (
        (answer.get("success") is True and isinstance(answer.get("fill"), dict)
         and (failed is None or isinstance(answer.get("failed_plate"), dict)))
        or (isinstance(answer.get("error"), dict) and answer["error"].get("code"))
        or answer.get("required_tier")
    ):
        return answer
    miss = served_answer.classify_answer(answer)
    return _miss(miss or served_answer.Miss("unanswered", detail="no plan in the answer"), failed=failed is not None)


def _miss(miss: Any, *, failed: bool = False) -> dict[str, Any]:
    """The refusal when the plan could not be had: what Kiln could not do,
    why, and that nothing was planned or queued.  For a failed plate: that
    nothing moves, and the copies print again where they were."""
    import kiln.server as _srv
    from kiln import served_answer

    if failed:
        text = served_answer.sentence(
            miss,
            feature="moving a failed plate's copies",
            on_the_line="The answer says whether the copies print again here, move to another printer, or stop",
            cannot="ask Kiln's servers where a failed plate's copies should go",
            wont="won't move any copies to another printer",
            safe_remedy="queue them again on the same printer, as it does on its own",
        )
    else:
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
