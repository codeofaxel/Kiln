"""Public-Kiln → kiln-pro motion bridge: where a head-motion PLAN comes from.

Public Kiln owns the doors, the floor, and the hands: the ``home_axes`` /
``park_head`` / ``wipe_nozzle`` / ``purge_filament`` templates, step mode,
the consent rule for a Z home that presses the nozzle onto the plate, the
plate record, the heater-off finish, every refusal's wording -- and the
executor that sends a plan's lines to the printer and reports what
happened (:mod:`kiln.printers.motion_plan`).  What it does not own is the
plan for a given model: how that machine is raised, parked over its chute,
wiped on its pad and homed in its maker's own order.  That is a per-model
record and a reading of the maker's files, kept by kiln-pro and handed
over one plan at a time, for one verb on one machine.

A plan reaches the executor from one of three places, tried in order:

1. **kiln-pro importable** (a source-tree install with the overlay on disk):
   ``kiln_pro.motion.build_plan`` builds it from the local overlay.
2. **served**: the signed-in user's Kiln asks the hosted service for the
   plan (``POST /api/tools/motion_plan``), sending the printer's model and
   serial.  The service answers only for a machine that install has
   reported (its heartbeat names the model), caps how many distinct
   models one device may be served, and logs every serve.  Free accounts
   are served; a fleet of machines is the paid axis, as it already is.
3. **cached**: the last served plan for that machine, kept on disk
   encrypted with a key derived from the sign-in, so a printer already
   paired keeps homing when the network is down.  The cache holds plans
   for machines this install has been served for -- never anyone else's
   -- and it stands in only when the service did not ANSWER: a refusal
   from the service (an unpaired machine, the per-device cap) is a
   revocation, so the cached copy for that request is dropped, not
   served.

None of the three found → ``None``, and the public floor answers: a
Bambu model refuses to home, park or wipe by name and says what to use
instead; a purge runs in place and says so.  Never a stub that claims
success.  Nothing here gates a tier; entitlement is the service's.

**Why there is no plan** is kept too.  Four things can stop a served
plan and each has a different fix -- this computer is offline, Kiln is
signed out, the service did not answer, the service refused -- so a door
that has to refuse a motion can say which (:func:`miss_for`, a
:class:`kiln.served_answer.Miss`).  The line between "did not answer" and
"refused" is the one that matters to the cache: a transport failure, a
missing sign-in, and the service's own "try again shortly" (its heartbeat
table or counter was down) are not rulings on this machine, and a plan
that was true before one of them is still true after it.  Only an answer
that rules on the machine -- not paired, no record, over the cap -- drops
the cached copy.  :mod:`kiln.served_answer` draws that line for every
served door; nothing here decides it twice.

Plan document (``schema: "motion_plan/1"``), the contract both sides pin:

* ``printer_id``, ``verb`` (``home`` / ``park`` / ``wipe`` / ``purge``),
  ``ok``; when ``ok`` is false, ``refusal: {"code", "message"}`` carries the
  record's own reason and the executor wraps it in the public wording.
* ``steps``: list of ``{number, label, you_will_see, stops_when, gcode,
  leaves, touches_plate}`` -- the whole sequence; step mode is local.
* ``homed_axes``, ``heats_nozzle_to_c``, ``sequence_source``, ``summary``
  (the full-run sentence), ``resting_position``, ``homed_flag_bits``
  (axis → bit index, or ``None``), ``raise_clearance_mm``,
  ``z_home_on_plate`` (the Z step presses the plate: consent applies).
* purge / wipe: ``pre_gcode`` (sent before the heater), ``post_gcode``
  (rides after the extrude, before ``M82``), ``after`` (the sentence),
  ``placement`` (the ``purge_station`` dict), ``watch_seconds``,
  ``end_retract_mm``, ``details`` (``wipe_c``, ``done_below_c``, ...).
* ``finish``: ``{fan_on, handoff_c, timeout_s, fan_off, over_chute}`` --
  the cool-down the maker's own start sequence runs after a hot op; the
  executor runs it after the heater goes off.

Requests name the machine: ``printer_id`` (catalogue model), ``serial``,
``verb``, ``axes``, ``on_plate_ok`` (the person's consent was given, so a
Z home onto the plate may be planned).  Every function here returns
``None`` when nothing answers, so a caller branches on one value and
never on an import.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

SCHEMA = "motion_plan/1"
VERBS = ("home", "park", "wipe", "purge")

#: After the service could not be reached, it is not asked again for this
#: long: one door (``kiln doctor``) asks for five plans in a row, and five
#: thirty-second timeouts on a machine that is simply offline would make
#: the offline case -- the one the cache exists for -- the slowest of all.
SERVICE_BACKOFF_S: float = 60.0
_service_down_until: float = 0.0
#: Why the service is being left alone, so a door asked during the backoff
#: hears the same cause the first ask did, not a blank.
_service_down_miss: Miss | None = None
_UNREACHABLE_CODES = frozenset({"SERVER_UNREACHABLE", "KILN_API_HTTP_ERROR"})

#: Why the last ask for each request came back without a plan, keyed by
#: :func:`_key`.  Written by :func:`_served_plan`, cleared by a served
#: plan, read by :func:`miss_for`.  A cache hit leaves the entry in place:
#: the door has its plan, and the executor notes where it came from.
_misses: dict[tuple[str, str, str, str, bool], Miss] = {}


def _local_pro() -> Any | None:
    """``kiln_pro.motion`` when it is installed, else ``None``.

    Imported on every call rather than cached at import time: the public
    package is imported before an embedding host has decided whether the
    overlay is present, and a test that installs or removes the pro
    package mid-process must see the change.
    """
    try:
        from kiln_pro import motion  # type: ignore[import-not-found]
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 -- a broken pro install degrades to the floor
        logger.debug("kiln_pro.motion failed to import; serving the public floor", exc_info=True)
        return None
    return motion


def available() -> bool:
    """True when kiln-pro is importable here.  Not "a plan is obtainable":
    a served or cached plan needs no kiln-pro; ask :func:`plan_for`."""
    return _local_pro() is not None


def _is_plan(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and doc.get("verb") in VERBS


def _machine_request(adapter: Any, verb: str, axes: str, on_plate_ok: bool) -> dict[str, Any]:
    model = str(getattr(adapter, "_printer_model", "") or "").strip().lower()
    serial = str(getattr(adapter, "serial", "") or getattr(adapter, "_serial", "") or "").strip()
    return {"printer_id": model, "serial": serial, "verb": verb, "axes": axes, "on_plate_ok": bool(on_plate_ok)}


def _key(request: dict[str, Any]) -> tuple[str, str, str, str, bool]:
    return (
        str(request.get("printer_id") or ""), str(request.get("serial") or ""),
        str(request.get("verb") or ""), str(request.get("axes") or ""), bool(request.get("on_plate_ok")),
    )


def plan_for(adapter: Any, verb: str, *, axes: str = "XYZ", on_plate_ok: bool = False) -> dict[str, Any] | None:
    """The plan document for *verb* on *adapter*'s machine, or ``None``.

    Local kiln-pro first, then the service, then the cache -- see the
    module docstring.  A served plan is written to the cache on the way
    back; a cached plan is served only for the same machine, and carries
    ``from_cache`` and ``cache_because`` (why the service was not the
    source this time).  A plan that says ``ok: false`` is still a plan:
    the record's own refusal reason, handed back for the door to word.
    ``None`` leaves the reason in :func:`miss_for`.
    """
    request = _machine_request(adapter, verb, axes, on_plate_ok)
    if not request["printer_id"]:
        return None
    pro = _local_pro()
    if pro is not None and hasattr(pro, "build_plan"):
        try:
            doc = pro.build_plan(**request)
            if _is_plan(doc):
                return doc
        except Exception:  # noqa: BLE001 -- a local builder fault falls through to the service
            logger.debug("kiln_pro.motion.build_plan raised; asking the service", exc_info=True)
    from kiln.printers import motion_plan_cache as _cache

    doc = _served_plan(request)
    if _is_plan(doc):
        _cache.store(request, doc)
        return doc
    miss = _misses.get(_key(request))
    if miss is not None and miss.cause == "refused":
        _cache.forget(request)
        return None
    doc = _cache.load(request)
    if _is_plan(doc):
        doc = dict(doc)
        doc["from_cache"] = True
        if miss is not None:
            doc["cache_because"] = miss.cause
        return doc
    return None


def miss_for(adapter: Any, verb: str, *, axes: str = "XYZ", on_plate_ok: bool = False) -> Miss | None:
    """Why the last :func:`plan_for` for this request came back without a
    served plan, or ``None`` when it was served (or never asked)."""
    return _misses.get(_key(_machine_request(adapter, verb, axes, on_plate_ok)))


def _back_off(miss: Miss) -> None:
    global _service_down_until, _service_down_miss
    _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
    _service_down_miss = miss


def _served_plan(request: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the hosted service for the plan; ``None`` when it does not answer
    or answers no, with why in :data:`_misses` either way.

    Goes through the same door every served tool uses
    (``kiln.server._pro_api_call``): the user's sign-in, the device
    fingerprint header, the client version.  The service's own message is
    kept on the miss for the door to quote; the public floor words the
    refusal the user sees.
    """
    key = _key(request)
    if time.monotonic() < _service_down_until:
        if _service_down_miss is not None:
            _misses[key] = _service_down_miss
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        _misses[key] = Miss("unanswered", detail="the served door could not be opened on this install")
        return None
    try:
        answer = _pro_api_call("motion_plan", **request)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a motion
        logger.debug("motion_plan request failed", exc_info=True)
        miss = classify_transport_error(exc)
        _back_off(miss)
        _misses[key] = miss
        return None
    if not isinstance(answer, dict):
        _misses[key] = Miss("unanswered", detail="not an answer")
        return None
    doc = answer.get("plan") if "plan" in answer else answer
    if _is_plan(doc):
        _misses.pop(key, None)
        return doc
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer with no plan in it")
    if miss.code or miss.detail:
        logger.info("motion_plan not served (%s): %s", miss.cause, miss.detail or miss.code)
    if miss.code in _UNREACHABLE_CODES:
        _back_off(miss)
    _misses[key] = miss
    return None


def station_supports(adapter: Any, station: dict[str, Any] | None, capability: str) -> tuple[bool, str] | None:
    """``(ok, why)`` from local kiln-pro's gate, or ``None`` when it is not here.

    Used by the doors and ``kiln doctor`` to say in one sentence what this
    model can do; without local kiln-pro the answer comes from the plan
    document's ``ok`` / ``refusal`` instead (see :func:`plan_for`).
    """
    pro = _local_pro()
    if pro is None or not hasattr(pro, "station_supports"):
        return None
    try:
        answer = pro.station_supports(station, capability)
    except Exception:  # noqa: BLE001
        logger.debug("kiln_pro.motion.station_supports raised", exc_info=True)
        return None
    if answer is None:
        return None
    return bool(answer[0]), str(answer[1])
