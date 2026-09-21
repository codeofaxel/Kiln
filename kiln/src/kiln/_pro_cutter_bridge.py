"""Public Kiln -> kiln-pro filament-cutter bridge: report a cut, ask nothing.

A machine with a filament cutter cuts on a load, on a colour change, and on
some machines on the firmware's own cancel; no maker shows a cut counter on
any screen.  Kiln counts, at the three events it can honestly see, and this
file is the one place the printer doors report them:

* a print STARTED -- the sliced file's own ``total filament change`` line
  and its grams, from the chokepoint every start passes through
  (:meth:`PrinterAdapter.start_print`), so a print counts whether or not
  anything watches it finish;
* Kiln's own load, unload and cancel -- one report each, from the doors
  that send them;
* a tray change the open connection saw that nothing Kiln did explains --
  a load from the touchscreen, a print started from the maker's app.

What a report MEANS for a model -- whether a cancel cuts on it, whether it
has a cutter at all -- is kiln-pro's per-model table, never decided here.
Nothing in this file names a maker or a model.

Contract, the same as the motion bridge: kiln-pro importable (a source
install) -> local call; otherwise the signed-in user's Kiln POSTs the same
report to the hosted service over the door every served tool uses
(``kiln.server._pro_api_call``), off the calling thread so a print start
never waits on the network; nothing reachable -> the report is dropped and
nothing raises.  A counter that breaks a print is worse than one that
misses a cut.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

WIRE_TOOL = "record_cutter_events"

#: After the hosted service fails to answer, how long the bridge stops
#: trying.  Same backoff the motion bridge uses.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
#: Why the service is being left alone, so a status asked during the
#: backoff hears the same cause the first ask did.
_service_down_miss: Any = None
#: Why the last blade status for each machine had no answer (a
#: :class:`kiln.served_answer.Miss`), cleared by an answer.  A pre-flight
#: reads it to say what it could not check.
_last_miss: dict[str, Any] = {}

#: How much of a sliced file to scan for the slicer's totals comment.
#: Bambu Studio and Orca write ``; total filament change = N`` in the
#: footer block; the head is scanned too for slicers that front-load it.
_HEAD_LINES = 200
_TAIL_LINES = 600

#: ``; total filament change = 167`` (Bambu Studio / OrcaSlicer).  Absent on
#: a single-colour file, which plans no change.
_FILAMENT_CHANGE_RE = re.compile(r";\s*total\s+filament\s+change\s*[:=]\s*(?P<n>\d+)", re.IGNORECASE)
#: ``; total filament weight [g] : 3.89`` / ``; filament used [g] = 12.3``.
_FILAMENT_GRAMS_RE = re.compile(
    r";\s*(?:total\s+)?filament\s+(?:used|weight)\s*\[g\]\s*[:=]\s*(?P<values>[\d.\s,]+)",
    re.IGNORECASE,
)

#: How many days of the local event log a status request carries along.
FAULT_WINDOW_DAYS: int = 30
_FAULT_LOG_LIMIT = 1000


def available() -> bool:
    """True when kiln-pro is importable here (a source install)."""
    try:
        import kiln_pro.cutter_intelligence  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# reading the sliced file
# ---------------------------------------------------------------------------


def _head_and_tail(path: str) -> list[str]:
    head: list[str] = []
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i < _HEAD_LINES:
                head.append(line)
            tail.append(line)
    return head + list(tail)


def planned_cuts_in_file(file_path: str | None) -> int | None:
    """The filament changes the slicer planned, or ``None`` when unreadable.

    ``0`` for a file that carries no change line: a single-colour print
    plans no change.  ``None`` only when the file could not be read at all,
    so a caller can tell "no changes" from "no idea".
    """
    if not file_path or not isinstance(file_path, str):
        return None
    try:
        lines = _head_and_tail(file_path)
    except OSError:
        return None
    for line in lines:
        m = _FILAMENT_CHANGE_RE.search(line)
        if m:
            return int(m.group("n"))
    return 0


def grams_in_file(file_path: str | None) -> float | None:
    """The slicer's own grams for the file, or ``None``."""
    if not file_path or not isinstance(file_path, str):
        return None
    try:
        lines = _head_and_tail(file_path)
    except OSError:
        return None
    for line in lines:
        m = _FILAMENT_GRAMS_RE.search(line)
        if not m:
            continue
        total = 0.0
        found = False
        for part in m.group("values").replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                total += float(part)
                found = True
            except ValueError:
                continue
        if found:
            return total
    return None


# ---------------------------------------------------------------------------
# the three reports
# ---------------------------------------------------------------------------


def local_sliced_path(file_name: str | None) -> str | None:
    """The file on this disk behind a printer-side name, or ``None``.

    A start names the file as the PRINTER knows it (``part.gcode.3mf``);
    the totals line lives in the G-code Kiln sliced, which the slice
    ledger joins back to that name (the same join the Monitor's twin and
    the plate record use).  A name that is already a readable path is
    taken as it is.
    """
    if not file_name or not isinstance(file_name, str):
        return None
    try:
        from kiln.monitor_twin import sliced_entry_for

        entry = sliced_entry_for(file_name)
    except Exception:  # noqa: BLE001 -- a ledger miss is not a failure
        entry = None
    if entry:
        for key in ("output", "wrapped"):
            path = entry.get(key)
            if isinstance(path, str) and path and os.path.isfile(path):
                return path
    return file_name if os.path.isfile(file_name) else None


def record_print_cuts(printer_name: str, file_name: str | None, *, printer_model: str | None = None) -> int | None:
    """A print started: charge its planned changes and grams.  Never raises.

    Returns the planned change count when the file could be read, so the
    door can hold it and reconcile it against what the wire shows when the
    print ends; ``None`` when the file was not readable here.
    """
    path = local_sliced_path(file_name)
    planned = planned_cuts_in_file(path)
    grams = grams_in_file(path)
    if not planned and not grams:
        return planned
    _send(
        printer_name,
        printer_model=printer_model,
        planned_cuts=int(planned or 0),
        grams=float(grams or 0.0),
        # One start, one charge -- but the same file printed again later
        # is another print, so the key carries the moment as well.
        dedupe_key=f"start:{printer_name}:{file_name}:{time.time():.0f}",
    )
    return planned


def record_print_reconciliation(
    printer_name: str,
    *,
    job: str,
    planned: int,
    observed: int,
    printer_model: str | None = None,
) -> None:
    """A print Kiln started has ended: what it was charged versus what the wire showed.

    Both numbers travel; kiln-pro keeps them side by side and moves the
    answer's confidence word, never the count.  Never raises.
    """
    if not job:
        return
    _send(
        printer_name,
        printer_model=printer_model,
        reconcile_job=str(job),
        reconcile_planned=max(0, int(planned)),
        reconcile_observed=max(0, int(observed)),
        dedupe_key=f"reconcile:{printer_name}:{job}",
    )


def record_command_cut(printer_name: str, verb: str, *, printer_model: str | None = None) -> None:
    """Kiln sent a load / unload / cancel: report the verb.  Never raises."""
    _send(printer_name, printer_model=printer_model, command=str(verb), dedupe_key=f"{verb}:{printer_name}:{time.time():.3f}")


def record_observed_switch(
    printer_name: str,
    *,
    printer_model: str | None = None,
    from_tray: Any = None,
    to_tray: Any = None,
    verified: bool = True,
) -> None:
    """The machine changed its feeding slot on its own.  Never raises.

    ``verified`` says whether the field it was read from has been proven on
    hardware for that backend (the MQTT stream's own tray field is; a slot
    field discovered on a polled backend is not).  An unverified change is
    kept apart and counted only once the machine's own prints have agreed
    with the field.
    """
    payload: dict[str, Any] = {"observed_switches": 1} if verified else {"observed_switches_unverified": 1}
    _send(
        printer_name,
        printer_model=printer_model,
        dedupe_key=f"switch:{printer_name}:{from_tray}->{to_tray}:{time.time():.1f}",
        **payload,
    )


def _send(printer_name: str, **payload: Any) -> None:
    if not printer_name or not isinstance(printer_name, str):
        return
    if payload.get("printer_model") is None:
        payload["printer_model"] = _declared_model(printer_name)
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        from kiln_pro.cutter_intelligence.counter import record_cut_events

        record_cut_events(printer_name, **payload)
        return
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- the local counter never breaks a door
        logger.debug("local cutter counter failed", exc_info=True)
        return
    threading.Thread(
        target=_served_report,
        args=(printer_name, payload),
        name="kiln-cutter-report",
        daemon=True,
    ).start()


def _served_report(printer_name: str, payload: dict[str, Any]) -> None:
    """POST the report to the hosted service.  Runs off the calling thread."""
    global _service_down_until
    if time.monotonic() < _service_down_until:
        return
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return
    try:
        answer = _pro_api_call(WIRE_TOOL, printer_id=printer_name, **payload)
    except Exception:  # noqa: BLE001 -- the network is a degrade, never a print
        logger.debug("cutter report not served", exc_info=True)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        return
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S


def _declared_model(printer_name: str) -> str | None:
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for

        return (resolve_printer_model_for(printer_name) or "").strip().lower() or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# the one line at print start
# ---------------------------------------------------------------------------

#: The words that earn a line at print start.  ``approaching`` is a nudge
#: to order a spare; the other three mean the blade wants looking at.
_ADVISORY_WORDS: frozenset[str] = frozenset({"approaching", "due", "overdue", "check_now"})

#: A print start must not wait on a slow network for an advisory.
_CONSULT_TIMEOUT_S: float = 4.0


def consult_blade(printer_name: str, *, printer_model: str | None = None) -> dict[str, Any] | None:
    """One line about this machine's blade, or ``None`` when there is nothing to say.

    Asks kiln-pro locally when it is here, the hosted door otherwise, with
    a short timeout; a machine with no cutter, an unknown model, a healthy
    blade, or no answer all read as ``None`` -- the print start says
    nothing rather than something it is not sure of.  ``{"word", "line",
    "confidence", "next_step"}`` when the blade wants attention.
    """
    if not printer_name or not isinstance(printer_name, str):
        return None
    model = printer_model or _declared_model(printer_name)
    answer: dict[str, Any] | None = None
    try:
        from kiln_pro.cutter_intelligence.catalogue import row_for_model
        from kiln_pro.cutter_intelligence.faults import faults_for
        from kiln_pro.cutter_intelligence.store_resolver import resolve_backend
        from kiln_pro.cutter_intelligence.wear import cutter_status

        backend, _nudge = resolve_backend(tool_name="cutter_wear_status")
        if backend is None:
            return None
        state = backend.get(printer_name)
        status = cutter_status(
            state, machine=printer_name, printer_model=model or (state.printer_model if state else None),
            row=row_for_model(model or (state.printer_model if state else None)),
            faults=faults_for(printer_name),
        )
        answer = status.to_dict()
    except ImportError:
        answer = _served_status(printer_name, model)
    except Exception:  # noqa: BLE001 -- an advisory never breaks a start
        logger.debug("blade consult failed", exc_info=True)
        return None
    if not isinstance(answer, dict) or answer.get("word") not in _ADVISORY_WORDS:
        return None
    return {
        "word": answer.get("word"),
        "line": str(answer.get("why") or ""),
        "confidence": str(answer.get("confidence") or ""),
        "next_step": str(answer.get("next_step") or ""),
    }


def _served_status(printer_name: str, model: str | None) -> dict[str, Any] | None:
    """The hosted blade status, or ``None`` with why in :data:`_last_miss`."""
    global _service_down_until, _service_down_miss
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    if time.monotonic() < _service_down_until:
        if _service_down_miss is not None:
            _last_miss[printer_name] = _service_down_miss
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        _last_miss[printer_name] = Miss("unanswered", detail="the served door could not be opened on this install")
        return None
    kwargs: dict[str, Any] = {"printer_id": printer_name, "recent_faults": recent_faults_for(printer_name)}
    if model:
        kwargs["printer_model"] = model
    try:
        answer = _pro_api_call("cutter_wear_status", _timeout=_CONSULT_TIMEOUT_S, **kwargs)
    except Exception as exc:  # noqa: BLE001
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        _last_miss[printer_name] = miss
        return None
    if isinstance(answer, dict) and answer.get("success"):
        _last_miss.pop(printer_name, None)
        return answer
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer with no status in it")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    _last_miss[printer_name] = miss
    return None


def blade_unchecked(printer_name: str) -> dict[str, Any] | None:
    """Why the last blade consult for *printer_name* could not be made, as
    the line a pre-flight carries, or ``None`` when it was answered.

    ``{"word": "unchecked", "line", "why", "why_code", "why_detail"}``.  A
    pre-flight is a checklist: a blade it could not ask about is named as
    such, so "not checked" never reads the same as "fine".  With kiln-pro
    installed the consult never misses, and this stays ``None``.
    """
    if not printer_name or not isinstance(printer_name, str):
        return None
    miss = _last_miss.get(printer_name)
    if miss is None:
        return None
    from kiln.served_answer import fields, sentence

    return {
        "word": "unchecked",
        "line": sentence(
            miss, feature="servers",
            on_the_line="On a printer with a filament cutter, this pre-flight says whether the blade is due",
            cannot=f"check {printer_name}'s blade", wont="says nothing about it",
            safe_remedy="Print as usual", then="run the pre-flight again",
        ),
        **fields(miss),
    }


# ---------------------------------------------------------------------------
# what a status request carries along
# ---------------------------------------------------------------------------


def recent_faults_for(printer_name: str, *, days: int = FAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
    """This machine's fault codes from the local event log, newest first.

    Public Kiln persists every printer fault it notices (the fault edge
    publishes an event; the server logs every event).  The hosted service
    runs no printers and has no log of anyone's, so a status request from
    this install sends the codes along and kiln-pro decides which belong
    to which part.  Raw codes and timestamps only.
    """
    try:
        from kiln.events import EventType
        from kiln.persistence import get_db

        # The bus persists each event under its enum VALUE ("printer.error"),
        # so the query names the enum rather than spelling the string twice.
        rows = get_db().recent_events(EventType.PRINTER_ERROR.value, limit=_FAULT_LOG_LIMIT)
    except Exception:  # noqa: BLE001 -- no log is no faults known
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    wanted = str(printer_name or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = row.get("data") or {}
        if not isinstance(data, dict):
            continue
        name = str(data.get("printer_name") or "").strip().lower()
        if wanted and name != wanted and str(row.get("source") or "") != f"printer:{printer_name}":
            continue
        try:
            ts = float(row.get("timestamp"))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        code = data.get("print_error_code") or data.get("code") or data.get("print_error")
        if not code:
            continue
        out.append({"code": str(code), "at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")})
    return out


def with_recent_faults(tool_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Attach this install's recent faults to a hosted blade-status request.

    ``cutter_wear_status`` takes one machine's list; ``maintenance_due``
    takes a map for the machines it names (or every registered one).  A
    caller that already supplied the field is left alone.
    """
    if tool_name == "cutter_wear_status":
        if "recent_faults" in kwargs or not kwargs.get("printer_id"):
            return kwargs
        return {**kwargs, "recent_faults": recent_faults_for(str(kwargs["printer_id"]))}
    if tool_name == "maintenance_due":
        if "recent_faults" in kwargs:
            return kwargs
        names = kwargs.get("printer_names")
        if not names:
            try:
                from kiln.registry import get_printer_registry

                names = list(get_printer_registry().list_machines())
            except Exception:  # noqa: BLE001
                names = []
        return {**kwargs, "recent_faults": {str(n): recent_faults_for(str(n)) for n in names}}
    return kwargs


__all__ = [
    "FAULT_WINDOW_DAYS",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "available",
    "blade_unchecked",
    "consult_blade",
    "grams_in_file",
    "local_sliced_path",
    "planned_cuts_in_file",
    "recent_faults_for",
    "record_command_cut",
    "record_observed_switch",
    "record_print_cuts",
    "record_print_reconciliation",
    "with_recent_faults",
]
