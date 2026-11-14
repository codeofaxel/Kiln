"""The resume files Kiln itself made, and for which paused job.

A start that continues a paused print goes ahead without asking the person
again, without a preview and without the print-time safety gate: the print
already had all three when it began.  What earns that is never what the
start says about itself (a ``resume_from_paused`` flag, a file named like a
resume file), and never only that the printer is paused, since anything that
can pause a running print could then start any file as a "resume".  It is a
record kept here:

* **Made by Kiln.**  :func:`record_made_resume` is called by the code that
  writes a resume file for a printer, and by the code that saves one Kiln's
  servers handed over for it.  It keeps the file's SHA-256, the machine, the
  job the printer was running or holding when the file was made, what made
  it, and when.  No tool writes a record and no tool argument names one.
* **Followed to the printer.**  Every upload a printer adapter makes passes
  through :func:`digest_for_upload` and :func:`note_upload` (the adapter's
  upload template), which ties a recorded file to the name it was saved
  under on that machine and unties the name when anything else is saved
  over it.
* **Asked at every start.**  :func:`made_for_paused_job` names the record a
  start is: the file being started is the printer-side name a recorded file
  was uploaded under through Kiln, with nothing saved over it since, its
  record is recent (:data:`RECENT_SECONDS`), and the job the printer reports
  now is the job it was made for.  A printer that reports no job gets no
  record, so nothing it starts is a resume.  :func:`kiln.print_signoff.verified_resume` adds the printer
  reporting a paused job, and is the one check every start door asks.

Records live in this process's memory, so only Kiln's own code writes one;
a restart forgets them, and a resume started after a restart is asked about
and previewed like a new print.  On Kiln's shared server nothing is recorded:
no printer is there.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "RECENT_SECONDS",
    "RESUME_MAKERS",
    "record_handed_resumes",
    "ResumeRecord",
    "digest_for_upload",
    "file_sha256",
    "made_for_paused_job",
    "note_upload",
    "paused_job_name",
    "record_made_resume",
    "records_for",
    "report_unrecognized",
]

#: How long after it was made a resume file may still continue its print
#: without a yes.  A pause held longer than this is asked about again.
RECENT_SECONDS = 6 * 60 * 60

#: The served tools whose job is to build a resume file for the job a
#: printer is holding.  A printable file Kiln's servers hand over in one of
#: their answers, or ask this computer to upload and start as a resume, is
#: recorded as Kiln's.  A tool not named here (one that only rewraps a file
#: the caller brought, for one) hands over files that are new prints.
RESUME_MAKERS: frozenset[str] = frozenset(
    {
        "add_feature_during_print",
        "add_feature_during_print_fleet",
        "apply_mid_print_decoration_plan",
        "auto_recover",
        "deboss_during_print",
        "decorate_during_print",
        "decorate_during_print_fleet",
        "get_print_before_edit",
        "get_print_before_edit_fleet",
        "plan_fleet_material_change",
        "plan_mid_print_decoration",
        "recover_power_loss_print",
        "resume_interrupted_print",
    }
)

_PRINTABLE = (".3mf", ".gcode", ".gco", ".g")

#: The most records kept at once; the oldest go first.
_MAX_RECORDS = 64

#: How long one refused claim on one machine is reported once, however many
#: of a start's gates ask about it.
_REPORT_QUIET_SECONDS = 120.0

_CHUNK = 1024 * 1024


@dataclass
class ResumeRecord:
    """One resume file Kiln made for one machine's job."""

    sha256: str
    path: str
    machine: str
    printer_name: str
    job: str
    made_by: str
    session: str
    made_at: float
    #: Printer-side names (lowercased base names) this file was uploaded
    #: under on its machine, and nothing has been saved over since.
    uploaded_as: set[str] = field(default_factory=set)

    def is_recent(self, now: float | None = None) -> bool:
        return 0 <= (now if now is not None else time.time()) - self.made_at <= RECENT_SECONDS


_lock = threading.Lock()
_records: list[ResumeRecord] = []
_reported: dict[tuple[str, str], float] = {}


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unreadable is a local install
        return False


def _name(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return (text.rsplit("/", 1)[-1] or text).lower()


def file_sha256(path: Any) -> str | None:
    """The SHA-256 of the file at *path*, or ``None`` when it cannot be read."""
    try:
        if not path or not os.path.isfile(str(path)):
            return None
        digest = hashlib.sha256()
        with open(str(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _machine(adapter: Any) -> str:
    from kiln.registry import machine_fingerprint

    return machine_fingerprint(adapter)


def _resolve(adapter: Any, printer_name: str | None) -> Any:
    if adapter is not None:
        return adapter
    from kiln.print_signoff import find_printer

    return find_printer(printer_name)


def paused_job_name(adapter: Any) -> str:
    """The base name of the job *adapter*'s printer reports, lowercased, or
    ``""`` when it reports none or cannot be read."""
    try:
        job = adapter.get_job()
    except Exception:  # noqa: BLE001 -- unreadable is unknown
        return ""
    return _name(getattr(job, "file_name", None))


def record_made_resume(
    path: str,
    *,
    printer_name: str | None = None,
    adapter: Any = None,
    made_by: str,
    session: str = "",
) -> ResumeRecord | None:
    """Record that Kiln made the resume file at *path* for the job the
    printer (*adapter*, else the one *printer_name* names) is running or
    holding now.  ``None`` when nothing was recorded: on the shared server,
    for a file that cannot be read, a printer that cannot be found, or one
    that reports no job (a resume of no known job continues nothing).
    Never raises."""
    if _hosted():
        return None
    digest = file_sha256(path)
    if digest is None:
        return None
    try:
        adapter = _resolve(adapter, printer_name)
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001 -- no printer, no record
        logger.debug("resume record: printer not found for %s", path, exc_info=True)
        return None
    job = paused_job_name(adapter)
    if not job:
        logger.info("resume record: %s not recorded, the printer reports no job", os.path.basename(str(path)))
        return None
    record = ResumeRecord(
        sha256=digest,
        path=str(path),
        machine=machine,
        printer_name=str(getattr(adapter, "_kiln_registered_name", None) or printer_name or ""),
        job=job,
        made_by=str(made_by or ""),
        session=str(session or ""),
        made_at=time.time(),
    )
    with _lock:
        _records[:] = [r for r in _records if not (r.machine == machine and r.sha256 == digest)]
        _records.append(record)
        del _records[:-_MAX_RECORDS]
    return record


def record_handed_resumes(tool: str, answer: Any, *, printer_name: str | None = None) -> int:
    """Record each printable file Kiln's servers handed over in *answer* as
    a resume file Kiln made, when *tool* is one of :data:`RESUME_MAKERS`.

    The printer is the one the file's place in the answer names (a fleet
    answer files each printer's files under its name), else *printer_name*,
    the printer the call was aimed at.  Returns how many were recorded.
    Never raises."""
    if tool not in RESUME_MAKERS or not isinstance(answer, dict):
        return 0
    files = answer.get("files")
    if not isinstance(files, list):
        return 0
    count = 0
    for item in files:
        if not isinstance(item, dict) or not item.get("on_this_computer"):
            continue
        path = str(item.get("path") or "")
        if not path.lower().endswith(_PRINTABLE):
            continue
        aimed = printer_name
        for key in item.get("at") or []:
            if isinstance(key, str) and key and _names_a_printer(key):
                aimed = key
                break
        if record_made_resume(path, printer_name=aimed, made_by=tool) is not None:
            count += 1
    return count


def _names_a_printer(name: str) -> bool:
    try:
        from kiln.registry import get_registry

        return name in set(get_registry().list_names())
    except Exception:  # noqa: BLE001
        return False


def records_for(adapter: Any) -> list[ResumeRecord]:
    """The recent records for *adapter*'s machine, newest first."""
    try:
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001
        return []
    now = time.time()
    with _lock:
        return [r for r in reversed(_records) if r.machine == machine and r.is_recent(now)]


def digest_for_upload(adapter: Any, local_path: Any) -> str | None:
    """The SHA-256 of a file about to be uploaded to *adapter*, read only
    when a record for that machine exists (otherwise ``None`` and nothing
    is read)."""
    try:
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001
        return None
    with _lock:
        if not any(r.machine == machine for r in _records):
            return None
    return file_sha256(local_path)


def note_upload(adapter: Any, remote_name: Any, *, digest: str | None) -> None:
    """After an upload to *adapter* under *remote_name*: a record whose file
    has *digest* gains that name, and every other record on the machine
    loses it (something else is saved there now).  *digest* ``None`` (the
    upload failed, or no record was asked about) only unties the name.
    Never raises."""
    name = _name(remote_name)
    if not name:
        return
    try:
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001
        return
    with _lock:
        for record in _records:
            if record.machine != machine:
                continue
            if digest is not None and record.sha256 == digest:
                record.uploaded_as.add(name)
            else:
                record.uploaded_as.discard(name)


def made_for_paused_job(file_name: str, *, adapter: Any) -> ResumeRecord | None:
    """The record that makes starting *file_name* on *adapter*'s machine a
    continuation of the job it reports now, or ``None``.

    The file must be the printer-side name a recorded file was uploaded
    under through Kiln, with nothing saved over that name since
    (:func:`note_upload`); a local copy that hashes the same is not the file
    the printer starts.  The record must be recent, and the printer must
    report the same job now that it reported when the file was made.
    Whether the printer is paused is asked by
    :func:`kiln.print_signoff.verified_resume`, not here.
    """
    name = _name(file_name)
    if not name:
        return None
    records = [r for r in records_for(adapter) if name in r.uploaded_as]
    if not records:
        return None
    job_now = paused_job_name(adapter)
    if not job_now:
        return None
    for record in records:
        if record.job == job_now:
            return record
    return None


def report_unrecognized(file_name: str, *, adapter: Any, why: str) -> None:
    """Say that a start on a paused printer claimed to continue its print
    with a file Kiln did not make for that job: in this computer's log, in
    the audit trail, and on the event bus a person's alerts read.  Once per
    file and machine in :data:`_REPORT_QUIET_SECONDS`.  Never raises."""
    try:
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001
        machine = ""
    printer = str(getattr(adapter, "_kiln_registered_name", None) or "")
    shown = os.path.basename(str(file_name or "")) or "a file"
    key = (machine, _name(file_name))
    now = time.monotonic()
    with _lock:
        last = _reported.get(key)
        if last is not None and now - last < _REPORT_QUIET_SECONDS:
            return
        _reported[key] = now
    logger.warning(
        "A start of %s said it continues the print paused on %s, and Kiln did not make that "
        "file for that job (%s). It is treated as a new print: the person is asked, it is "
        "previewed, and the print-time safety gate runs.",
        shown, printer or "this printer", why,
    )
    details = {"file": shown, "printer": printer, "why": why}
    # The server that owns the printers keeps the audit trail and the event
    # bus; a process that never loaded it has the log line above.
    server = sys.modules.get("kiln.server")
    try:
        if server is not None:
            server._audit("start_print", "resume_claim_refused", details=details)
    except Exception:  # noqa: BLE001
        logger.debug("resume claim: audit not written", exc_info=True)
    try:
        from kiln.events import Event, EventType

        _event_bus = getattr(server, "_event_bus", None)
        if _event_bus is not None:
            _event_bus.publish(
                Event(
                    type=EventType.SAFETY_BLOCKED,
                    data={"kind": "resume_claim_refused", **details},
                    source=f"resume:{printer or 'printer'}",
                )
            )
    except Exception:  # noqa: BLE001
        logger.debug("resume claim: event not published", exc_info=True)


def _reset_for_tests() -> None:
    with _lock:
        _records.clear()
        _reported.clear()
