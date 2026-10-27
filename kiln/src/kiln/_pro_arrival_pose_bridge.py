"""Public Kiln → Kiln's servers: which way up an arriving model stands.

:func:`kiln.arrival_pose.stand_up` measures a part that just arrived in each
of its six axis poses on this computer (:func:`kiln.arrival_pose.measure`)
and asks here whether it stands as written, should be turned, or cannot be
told.  The judgement is Kiln's and lives on its servers; it is free at
every tier, asked once per arrival, and nothing sent is kept.  Two ways in:

1. **kiln-pro on this computer** (Kiln's hosted servers): asked in process
   of the same function that answers the wire, imported by name in
   :func:`_local`, never by a string, so a static reader of the call graph
   sees the crossing.
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

What comes back is the answer, in words, and the turn to apply: never the
measurements' weights or the rule that read them.  A miss (offline, signed
out, no answer, refused) is a part shown exactly as it arrived, said once
in :func:`miss_sentence`.  Nothing here raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error, fields, sentence

logger = logging.getLogger(__name__)

#: The served tool that answers one arriving part's pose.
WIRE_TOOL = "arrival_pose"
#: Where the same judgement is worked out when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.arrival_pose"
#: The verdicts the answer may carry.
VERDICTS = ("turn", "keep", "unsure")
#: An arrival waits this long for the judgement before the part is shown as
#: it arrived.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the servers fail to answer, how long the judgement is not asked for.
SERVICE_BACKOFF_S: float = 300.0

_service_down_until: float = 0.0


@dataclass(frozen=True)
class Verdict:
    """Kiln's judgement of one arriving part: ``verdict`` (one of
    :data:`VERDICTS`), ``up`` (which axis of the file is up, when a turn is
    named), ``turn`` (``rotate_model``'s axes, or ``None``), ``said`` (the
    sentence for the person) and ``candidates`` (the poses it could stand
    in, when unsure)."""

    verdict: str
    up: str | None
    turn: dict[str, float] | None
    said: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    #: The same turn as advice, for a door that will not apply it.
    advice: str = ""


def judge(facts: dict[str, Any]) -> tuple[Verdict | None, Miss | None]:
    """Kiln's judgement of *facts* (:func:`kiln.arrival_pose.measure_file`'s
    answer), or ``(None, miss)`` saying why there is none.  Never raises."""
    local = _local(facts)
    if local is not _NOT_HERE:
        return local
    return _served(facts)


def miss_sentence(miss: Miss | None) -> str:
    """What the person is told when the judgement did not arrive."""
    if miss is None:
        miss = Miss("unanswered", detail="no judgement")
    return sentence(
        miss,
        feature="pose check",
        on_the_line="Which way up a model stands is checked on Kiln's servers",
        cannot="check which way up this one goes",
        wont="is shown exactly as it arrived",
        safe_remedy="if it lies wrong, turn it with rotate_model",
        then="bring it in again",
    )


def miss_fields(miss: Miss | None) -> dict[str, str]:
    """The machine-readable twin of :func:`miss_sentence`."""
    return fields(miss)


#: ``_local``'s answer when kiln-pro is not on this computer.
_NOT_HERE = object()


def _local(facts: dict[str, Any]) -> Any:
    """kiln-pro's judgement of *facts*, asked here exactly as the wire asks it."""
    try:
        from kiln_pro.arrival_pose import judge_for_the_wire
    except ImportError:
        return _NOT_HERE
    try:
        answer = judge_for_the_wire(facts)
    except Exception as exc:  # noqa: BLE001 -- the part is shown as it arrived
        from kiln.served_answer import local_fault

        return None, local_fault(exc, where="the pose check on this computer")
    verdict = _verdict_in(answer)
    if verdict is None:
        return None, classify_answer(answer) or Miss("unanswered", detail="no verdict")
    return verdict, None


def _served(facts: dict[str, Any]) -> tuple[Verdict | None, Miss | None]:
    if time.monotonic() < _service_down_until:
        return None, Miss("unanswered", code="SERVER_UNREACHABLE", detail="not asked again yet")
    try:
        from kiln.server import _pro_api_call
    except Exception as exc:  # noqa: BLE001
        return None, Miss("unanswered", detail=str(exc)[:120])
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # Kiln asks this of its own accord as a model arrives; the
            # measurements are of the part, never of this computer.
            _asked_by_user=False,
            facts=facts,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        miss = classify_transport_error(exc)
        _note_miss(miss)
        return None, miss
    verdict = _verdict_in(answer)
    if verdict is None:
        miss = (classify_answer(answer) if isinstance(answer, dict) else None) or Miss("unanswered", detail="no verdict")
        _note_miss(miss)
        return None, miss
    return verdict, None


def _verdict_in(answer: Any) -> Verdict | None:
    """The verdict in an answer, from kiln-pro here or from the wire;
    ``None`` unless it is known and well formed.  Only these fields are
    read, so nothing else the answer carried can reach a reply."""
    if not (isinstance(answer, dict) and answer.get("success") is True and answer.get("known") is True):
        return None
    verdict = answer.get("verdict")
    said = answer.get("said")
    if verdict not in VERDICTS or not isinstance(said, str) or not said.strip():
        return None
    turn = _turn_in(answer.get("turn"))
    if verdict == "turn" and turn is None:
        return None
    candidates = []
    for item in answer.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        c_turn = _turn_in(item.get("turn"))
        c_said = item.get("said")
        if c_turn is None or not isinstance(c_said, str):
            continue
        candidates.append({"up": str(item.get("up") or ""), "turn": c_turn, "said": c_said})
    up = answer.get("up")
    advice = answer.get("advice")
    return Verdict(
        verdict=str(verdict),
        up=str(up) if isinstance(up, str) and up else None,
        turn=turn if verdict == "turn" else None,
        said=said.strip(),
        candidates=candidates,
        advice=advice.strip() if isinstance(advice, str) else "",
    )


def _turn_in(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        try:
            deg = float(value.get(axis, 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if deg != deg or abs(deg) > 360.0:
            return None
        out[axis] = deg
    return out if any(out.values()) else None


def _note_miss(miss: Miss | None) -> None:
    global _service_down_until
    if miss is not None and miss.cause in ("offline", "unanswered"):
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    logger.debug("arrival pose not judged (%s); shown as it arrived", miss and miss.cause)
