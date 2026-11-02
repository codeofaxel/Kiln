"""Public Kiln → Kiln's servers: the failure warning while a print runs.

While Kiln watches a print (:class:`kiln.print_health_monitor.PrintHealthMonitor`)
it asks whether the printer's readings are drifting the way they do before a
print fails.  With kiln-pro on this computer the watcher asks it in process;
on any other install it asks Kiln's servers here, through the same served tool
an agent calls (:data:`WIRE_TOOL`), so every install gets the same answer:

* every plan is told the risk in one word (``clear``, ``watch``, ``high
  risk``) with what to do, below Kiln Pro on a daily number of prints;
* Kiln Pro is also told which reading is drifting, by how much and since when.

Kiln's servers keep the print's readings themselves and judge the trend from
them, so this sends only the printer's CURRENT reading, at the tool's own pace
(:data:`ASK_EVERY_S`), never a history.  Nothing here decides a tier.

A miss (offline, signed out, no answer, refused) is a print that is not being
warned about, and the watcher is told so in one sentence
(:func:`miss_answer`); a miss is never read as ``clear``.  Nothing here raises.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from kiln.served_answer import (
    SESSION_EXPIRED_CODE,
    Miss,
    classify_answer,
    classify_transport_error,
    fields,
    sentence,
)

logger = logging.getLogger(__name__)

#: The served tool that judges one print's readings.
WIRE_TOOL = "predict_print_failure_risk"
#: The tool counts at most one reading this often (its own contract: "call it
#: every 30 seconds"); asking sooner only repeats the answer it already has.
ASK_EVERY_S: float = 30.0
#: A reading waits this long for the answer before the watcher moves on.
_CONSULT_TIMEOUT_S: float = 5.0
#: After the servers fail to answer, how long the warning is not asked for.
SERVICE_BACKOFF_S: float = 300.0

#: The verdict words the servers answer with, and the severity the watcher's
#: one-shot summary reads them as.  Any other word (not enough readings yet,
#: not judged today, readings that can't be judged) is ``not_judged``: never
#: ``clear``.
_SEVERITY_OF_WORD = {"clear": "clear", "watch": "amber", "high risk": "red"}
NOT_JUDGED = "not_judged"
#: The issue a ``high risk`` word becomes when the answer names no reading
#: (below Kiln Pro): the watcher's issue stream reads ``predictive_red_<kind>``.
UNNAMED_KIND = "readings"

#: The reading keys the served tool judges; the rest of a watcher's reading
#: (its own timestamp, which the servers do not read) stays here.
_READING_KEYS = (
    "hotend_temp", "hotend_target", "bed_temp", "bed_target",
    "connected", "filament_detected", "flow_ratio",
    "vibration_rms", "acoustic_band_2k_4k",
)

_lock = threading.Lock()
_last_asked: dict[tuple[str, str], float] = {}
_service_down_until: float = 0.0


def warning_for(printer_id: str, job_id: str, reading: dict[str, Any]) -> dict[str, Any] | None:
    """Kiln's warning for the print *job_id* on *printer_id*, given its
    current *reading*, shaped for the watcher; ``None`` when it is not yet
    time to ask again.  A miss comes back as :func:`miss_answer`."""
    key = (str(printer_id or ""), str(job_id or ""))
    now = time.monotonic()
    with _lock:
        last = _last_asked.get(key)
        if last is not None and now - last < ASK_EVERY_S:
            return None
        _last_asked[key] = now
        down = now < _service_down_until
    if down:
        return miss_answer(Miss("unanswered", code="SERVER_UNREACHABLE", detail="not asked again yet"))
    sent = {k: reading[k] for k in _READING_KEYS if k in reading}
    if not sent:
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception as exc:  # noqa: BLE001
        return miss_answer(Miss("unanswered", detail=str(exc)[:120]))
    job_info = {"job_id": key[1]} if key[1] else None
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            _timeout=_CONSULT_TIMEOUT_S,
            # Kiln asks this of its own accord while it watches a print; the
            # reading is of the printer's heaters and sensors.
            _asked_by_user=False,
            telemetry=sent,
            job_info=job_info,
            printer_id=key[0] or None,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        miss = classify_transport_error(exc)
        _note_miss(miss)
        return miss_answer(miss)
    judged = _answer_in(answer)
    if judged is None:
        miss = (classify_answer(answer) if isinstance(answer, dict) else None) or Miss(
            "unanswered", detail="no warning in the answer"
        )
        _note_miss(miss)
        return miss_answer(miss)
    return judged


def issues_in(answer: dict[str, Any] | None) -> list[tuple[str, str]]:
    """``(issue_type, detail)`` for each red warning in *answer*: one per
    reading Kiln Pro names, else one for a ``high risk`` word."""
    if not isinstance(answer, dict) or answer.get("severity") != "red":
        return []
    named = [
        (f"predictive_red_{s['kind']}", str(s.get("message") or answer.get("said") or ""))
        for s in answer.get("signals") or []
        if s.get("severity") == "red" and s.get("kind")
    ]
    return named or [(f"predictive_red_{UNNAMED_KIND}", str(answer.get("said") or ""))]


def miss_answer(miss: Miss | None) -> dict[str, Any]:
    """What the watcher keeps when no warning arrived: never a verdict."""
    if miss is None:
        miss = Miss("unanswered", detail="no warning")
    stranger = miss.cause == "signed_out" and miss.code != SESSION_EXPIRED_CODE
    said = sentence(
        miss,
        feature="failure warnings",
        on_the_line="Warnings before a print fails come from Kiln's servers",
        cannot="judge this print's readings",
        wont="is not warning before this print fails",
        safe_remedy="keep an eye on the print",
        then=(
            "Kiln warns while it watches a print (a free account is enough)"
            if stranger
            else "Kiln picks up again on the next reading"
        ),
    )
    return {"severity": NOT_JUDGED, "risk": NOT_JUDGED, "said": said, "signals": [], **fields(miss)}


def _answer_in(answer: Any) -> dict[str, Any] | None:
    """The warning in a served answer, shaped for the watcher, or ``None``.
    Only these fields are read, so nothing else the answer carried (an upgrade
    block, an internal field) reaches the watcher's session."""
    if not isinstance(answer, dict) or answer.get("status") != "success":
        return None
    word = answer.get("risk")
    said = answer.get("said")
    if not isinstance(word, str) or not isinstance(said, str) or not said.strip():
        return None
    signals = [
        {
            "kind": str(s.get("kind")),
            "severity": str(s.get("severity") or ""),
            "message": str(s.get("message") or ""),
            "evidence": s.get("evidence"),
            "since": s.get("since"),
        }
        for s in answer.get("signals") or []
        if isinstance(s, dict) and s.get("kind")
    ]
    out: dict[str, Any] = {
        "severity": _SEVERITY_OF_WORD.get(word, NOT_JUDGED),
        "risk": word,
        "said": said.strip(),
        "signals": signals,
        "judged_by": "kiln_servers",
    }
    if isinstance(answer.get("readings_counted"), int):
        out["readings_counted"] = answer["readings_counted"]
    return out


def _note_miss(miss: Miss | None) -> None:
    global _service_down_until
    if miss is not None and miss.cause in ("offline", "unanswered"):
        with _lock:
            _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    logger.debug("print warning not judged (%s)", miss and miss.cause)


def _reset_for_tests() -> None:
    global _service_down_until
    with _lock:
        _last_asked.clear()
        _service_down_until = 0.0
