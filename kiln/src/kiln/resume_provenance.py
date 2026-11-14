"""The resume files Kiln itself made, from which bytes, and for which paused job.

A start that continues a paused print goes ahead without asking the person
again, without a preview and without the print-time safety gate: the print
already had all three when it began.  What earns that is never what the
start says about itself (a ``resume_from_paused`` flag, a file named like a
resume file), and never only that the printer is paused, since anything that
can pause a running print could then start any file as a "resume".  It is a
record kept here:

* **Made by Kiln, from the print's own bytes.**  :func:`try_record_resume` is
  the one call every resume maker makes: the code that writes a resume file
  for a printer, and the code that saves one Kiln's servers handed over.  The
  maker names the G-code it built the resume from (the file, or its SHA-256),
  and the record is made only when that is Kiln's own copy of the print the
  printer is holding: the toolpath Kiln kept when it started the job
  (:func:`recorded_job_source`).  A resume built from any other G-code,
  however it reached the maker, is not recorded; it is a new print, and the
  maker says so (:func:`not_recorded_sentence`).  A file Kiln rewrites from a
  recorded resume (a new picture inside it) is recorded through
  :func:`record_derived_resume`.  No tool writes a record and no tool
  argument names one.
* **Followed to the printer.**  Every upload a printer adapter makes passes
  through :func:`digest_for_upload` and :func:`note_upload` (the adapter's
  upload template), which ties a recorded file to the name it was saved
  under on that machine and to the bytes sent there.  Anything else uploaded
  under that name, a deleted file, and a raw command that writes or deletes
  a file on the printer untie it (:func:`forget_printer_names`).
* **Asked at every start.**  :func:`made_for_paused_job` names the record a
  start is: the file being started is the printer-side name a recorded file
  was uploaded under through Kiln, with nothing saved over it since; a
  printer that can hand a file back still holds those bytes under that name;
  the record is recent (:data:`RECENT_SECONDS`); and the job the printer
  reports now is the job it was made for.
  :func:`kiln.print_signoff.verified_resume` adds the printer reporting a
  paused job, and is the one check every start door asks.

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
    "SOURCE_KEY",
    "ResumeRecord",
    "digest_for_upload",
    "file_sha256",
    "forget_printer_names",
    "made_for_paused_job",
    "not_recorded_sentence",
    "note_raw_commands",
    "note_sent",
    "note_upload",
    "paused_job_name",
    "record_derived_resume",
    "record_handed_resumes",
    "record_made_resume",
    "recorded_job_source",
    "records_for",
    "report_unrecognized",
    "sent_sha256",
    "stated_source",
    "try_record_resume",
    "writes_a_file",
]

#: How long after it was made a resume file may still continue its print
#: without a yes.  A pause held longer than this is asked about again.
RECENT_SECONDS = 6 * 60 * 60

#: The served tools whose job is to build a resume file for the job a
#: printer is holding.  A printable file Kiln's servers hand over in one of
#: their answers, or ask this computer to upload and start as a resume, is
#: offered to the record (:func:`try_record_resume`), with the SHA-256 of the
#: G-code the servers say they built it from.  A tool not named here (one
#: that only rewraps a file the caller brought, for one) hands over files
#: that are new prints.
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

#: How long a printer's read-back of a resume file answers for every gate of
#: one start, so the file is read back once per start, not once per gate.
_READ_BACK_SECONDS = 30.0

#: Raw commands that write, delete or rename a file on the printer's own
#: storage: begin and end a write (M28 / M29), delete (M30), rename (M471),
#: write a configuration or web file (M559 / M560), and log to a file (M928).
_FILE_COMMANDS = frozenset({"M28", "M29", "M30", "M471", "M559", "M560", "M928"})

#: The key a resume maker's answer (or its upload step) carries for the
#: SHA-256 of the G-code it built the resume from.
SOURCE_KEY = "resume_source_sha256"

_CHUNK = 1024 * 1024


@dataclass
class ResumeRecord:
    """One resume file Kiln made for one machine's job, from that job's bytes."""

    sha256: str
    source_sha256: str
    path: str
    machine: str
    printer_name: str
    job: str
    made_by: str
    session: str
    made_at: float
    #: Printer-side names (lowercased base names) this file was uploaded
    #: under on its machine, with nothing saved over since, each with the
    #: SHA-256 of the bytes Kiln sent under it.
    uploaded_as: dict[str, str] = field(default_factory=dict)

    def is_recent(self, now: float | None = None) -> bool:
        return 0 <= (now if now is not None else time.time()) - self.made_at <= RECENT_SECONDS


_lock = threading.Lock()
_records: list[ResumeRecord] = []
_reported: dict[tuple[str, str], float] = {}
_read_back: dict[tuple[str, str, str], tuple[float, bool]] = {}
#: (machine, printer-side name) -> the SHA-256 of the bytes Kiln last sent
#: under that name, for every upload (:func:`note_sent`).
_sent: dict[tuple[str, str], str] = {}
_MAX_SENT = 256


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


def recorded_job_source(adapter: Any) -> tuple[str | None, str]:
    """``(sha256, "")`` of Kiln's own copy of the G-code of the job
    *adapter*'s printer reports now, or ``(None, why)``.

    Kiln's copy is the toolpath it kept when it started that job on that
    machine (:func:`kiln.monitor_twin.note_print_started`), named by its
    SHA-256 then.  A print Kiln did not start, or started from a file it did
    not slice, has no copy, and nothing can be resumed from it without a yes.
    """
    job_now = paused_job_name(adapter)
    if not job_now:
        return None, "the printer reports no job"
    try:
        machine = _machine(adapter)
        from kiln.monitor_twin import active_records

        records = active_records()
    except Exception:  # noqa: BLE001 -- unreadable is no record
        return None, "Kiln could not read its record of this printer's print"
    for record in records:
        if _name(record.get("file_name")) != job_now or str(record.get("machine") or "") != machine:
            continue
        digest = str(record.get("gcode_sha256") or "")
        if not digest:
            return None, (
                "Kiln started this print from a file it did not slice here, so it keeps no copy "
                "of the print's G-code to build a resume from"
            )
        return digest, ""
    return None, "Kiln did not start the print this printer is holding, so it keeps no copy of its G-code"


def try_record_resume(
    path: str,
    *,
    source: str | None = None,
    source_sha256: str | None = None,
    printer_name: str | None = None,
    adapter: Any = None,
    made_by: str,
    session: str = "",
) -> tuple[ResumeRecord | None, str]:
    """The one check every resume maker calls: record the resume file at
    *path* as Kiln's, for the job the printer (*adapter*, else the one
    *printer_name* names) is running or holding now, when it was built from
    Kiln's own copy of that job.

    *source* is the G-code the maker built the resume from, or
    *source_sha256* its digest (a served maker states it in its answer).
    ``(record, "")``, or ``(None, why)`` when nothing was recorded: the
    source is not Kiln's own copy of this print (or was not named), Kiln has
    no copy of the print, the printer reports no job or cannot be found, or
    the file cannot be read.  On the shared server nothing is recorded and
    nothing is said.  Never raises."""
    if _hosted():
        return None, ""
    digest = file_sha256(path)
    if digest is None:
        return None, "the resume file could not be read"
    try:
        adapter = _resolve(adapter, printer_name)
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001 -- no printer, no record
        logger.debug("resume record: printer not found for %s", path, exc_info=True)
        return None, "Kiln could not find the printer it was made for"
    job = paused_job_name(adapter)
    if not job:
        return None, "the printer reports no job, so there is no print for it to continue"
    stated = str(source_sha256 or "").strip().lower() or (file_sha256(source) if source else None)
    if not stated:
        return None, "the maker did not say which G-code it was built from"
    recorded, why = recorded_job_source(adapter)
    if recorded is None:
        return None, why
    if stated != recorded:
        return None, "it was built from G-code that is not Kiln's own copy of the print this printer is holding"
    return _keep(ResumeRecord(
        sha256=digest,
        source_sha256=recorded,
        path=str(path),
        machine=machine,
        printer_name=str(getattr(adapter, "_kiln_registered_name", None) or printer_name or ""),
        job=job,
        made_by=str(made_by or ""),
        session=str(session or ""),
        made_at=time.time(),
    )), ""


def record_made_resume(path: str, **kwargs: Any) -> ResumeRecord | None:
    """:func:`try_record_resume`, the record alone."""
    return try_record_resume(path, **kwargs)[0]


def record_derived_resume(
    path: str,
    *,
    from_path: str,
    printer_name: str | None = None,
    adapter: Any = None,
    made_by: str,
) -> tuple[ResumeRecord | None, str]:
    """Record *path*, a file Kiln rewrote from the recorded resume at
    *from_path* (the same resume with a new picture inside it), as the same
    printer's resume of the same job.  ``(None, why)`` unless *from_path* is
    a recent record for that machine and the printer still reports its job.
    Never raises."""
    if _hosted():
        return None, ""
    digest, earlier = file_sha256(path), file_sha256(from_path)
    if digest is None or earlier is None:
        return None, "the resume file could not be read"
    try:
        adapter = _resolve(adapter, printer_name)
    except Exception:  # noqa: BLE001
        return None, "Kiln could not find the printer it was made for"
    job_now = paused_job_name(adapter)
    parent = next((r for r in records_for(adapter) if r.sha256 == earlier and r.job == job_now), None)
    if parent is None or not job_now:
        return None, "it was rewritten from a file that is not a resume Kiln made for the print this printer is holding"
    return _keep(ResumeRecord(
        sha256=digest,
        source_sha256=parent.source_sha256,
        path=str(path),
        machine=parent.machine,
        printer_name=parent.printer_name,
        job=parent.job,
        made_by=str(made_by or ""),
        session=parent.session,
        made_at=time.time(),
    )), ""


def _keep(record: ResumeRecord) -> ResumeRecord:
    with _lock:
        _records[:] = [r for r in _records if not (r.machine == record.machine and r.sha256 == record.sha256)]
        _records.append(record)
        del _records[:-_MAX_RECORDS]
    return record


def not_recorded_sentence(file_name: str, printer: str | None, why: str) -> str:
    """What a maker says when the resume it made is not recorded: what the
    file is now, why, and what starting it takes."""
    shown = os.path.basename(str(file_name or "")) or "This file"
    on = f" on {printer}" if printer else ""
    return (
        f"{shown} is not a resume of the print{on}: {why}. Starting it is a new print: the "
        "person is asked, it is previewed, and the print-time safety gate runs."
    )


def stated_source(answer: Any, at: Any) -> str | None:
    """The source digest a served answer states for the file at *at* (its
    place in the answer): the one nearest the file, else the answer's own."""
    found = answer.get(SOURCE_KEY) if isinstance(answer, dict) else None
    block: Any = answer
    for key in at if isinstance(at, list) else []:
        if isinstance(block, dict) and isinstance(key, str):
            block = block.get(key)
        elif isinstance(block, list) and isinstance(key, int) and 0 <= key < len(block):
            block = block[key]
        else:
            break
        if isinstance(block, dict) and block.get(SOURCE_KEY):
            found = block.get(SOURCE_KEY)
    return str(found) if isinstance(found, str) and found else None


def record_handed_resumes(tool: str, answer: Any, *, printer_name: str | None = None) -> list[str]:
    """Offer each printable file Kiln's servers handed over in *answer* to
    the record (:func:`try_record_resume`), when *tool* is one of
    :data:`RESUME_MAKERS`, with the source digest the answer states for it
    (:func:`stated_source`).

    The printer is the one the file's place in the answer names (a fleet
    answer files each printer's files under its name), else *printer_name*,
    the printer the call was aimed at.  Returns the sentence for each file
    that was not recorded (:func:`not_recorded_sentence`).  Never raises."""
    notes: list[str] = []
    if tool not in RESUME_MAKERS or not isinstance(answer, dict):
        return notes
    files = answer.get("files")
    if not isinstance(files, list):
        return notes
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
        record, why = try_record_resume(
            path, source_sha256=stated_source(answer, item.get("at")), printer_name=aimed, made_by=tool,
        )
        if record is None and why:
            notes.append(not_recorded_sentence(path, aimed, why))
    return notes


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


def note_upload(adapter: Any, remote_name: Any, *, digest: str | None, sent: str | None = None) -> None:
    """After an upload to *adapter* under *remote_name*: a record whose file
    has *digest* gains that name, with *sent* (the SHA-256 of the bytes
    actually sent, when Kiln's checks sent a cleaned copy; else *digest*),
    and every other record on the machine loses it (something else is saved
    there now).  *digest* ``None`` (the upload failed, or no record was
    asked about) only unties the name.  Never raises."""
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
                record.uploaded_as[name] = sent or digest
            else:
                record.uploaded_as.pop(name, None)


def note_sent(adapter: Any, remote_name: Any, digest: str | None) -> None:
    """After an upload to *adapter* under *remote_name*: Kiln sent the bytes
    *digest* names there (``None``: the upload failed, so what the name holds
    is not known).  Never raises."""
    name = _name(remote_name)
    if not name:
        return
    try:
        key = (_machine(adapter), name)
    except Exception:  # noqa: BLE001
        return
    with _lock:
        _sent.pop(key, None)
        if digest:
            _sent[key] = digest
            while len(_sent) > _MAX_SENT:
                _sent.pop(next(iter(_sent)))


def sent_sha256(adapter: Any, file_name: Any) -> str | None:
    """The SHA-256 of the bytes Kiln last sent to *adapter*'s machine under
    *file_name*, or ``None`` when Kiln does not know what that name holds."""
    try:
        key = (_machine(adapter), _name(file_name))
    except Exception:  # noqa: BLE001
        return None
    with _lock:
        return _sent.get(key)


def forget_printer_names(adapter: Any, name: Any = None) -> None:
    """Untie *name* (every name, when ``None``) from the records of
    *adapter*'s machine: the file under it was deleted, or a raw command
    wrote or deleted a file there, so Kiln no longer knows what the name
    holds.  Never raises."""
    try:
        machine = _machine(adapter)
    except Exception:  # noqa: BLE001
        return
    one = _name(name) if name is not None else None
    with _lock:
        for key in [k for k in _sent if k[0] == machine and (one is None or k[1] == one)]:
            _sent.pop(key, None)
        for record in _records:
            if record.machine != machine:
                continue
            if one is None:
                record.uploaded_as.clear()
            else:
                record.uploaded_as.pop(one, None)


def writes_a_file(line: Any) -> bool:
    """Whether the raw command *line* would write, delete or rename a file on
    the printer (:data:`_FILE_COMMANDS`), read the way Kiln's G-code checks
    read a line: comment, line number and checksum taken off, and every
    command word on it judged, not only the first."""
    from kiln.gcode import _later_commands, _parse_command_word, _strip_comment, _strip_line_number

    cleaned = _strip_line_number(_strip_comment(str(line)))
    command = _parse_command_word(cleaned) if cleaned else None
    if command is None:
        return False
    words = [command] + [w for part in _later_commands(command, cleaned) if (w := _parse_command_word(part))]
    return any(word in _FILE_COMMANDS for word in words)


def note_raw_commands(adapter: Any, commands: Any) -> None:
    """Untie every name on *adapter*'s machine when any of *commands* (a raw
    command or a list of them, one or more per line) writes, deletes or
    renames a file on the printer (:func:`writes_a_file`).  A list Kiln
    cannot read unties them too.  Never raises."""
    try:
        lines = [commands] if isinstance(commands, str) else list(commands or [])
        if any(writes_a_file(part) for line in lines for part in str(line).splitlines()):
            forget_printer_names(adapter)
    except Exception:  # noqa: BLE001 -- unreadable is treated as a write
        logger.debug("resume record: raw commands not read; untying", exc_info=True)
        forget_printer_names(adapter)


def _printer_still_holds(adapter: Any, file_name: str, sent: str) -> bool:
    """Whether the printer still holds *sent* under *file_name*, read back
    from the printer when it can hand a file back (once per start).  A
    printer that cannot hand a file back is taken at Kiln's upload record;
    one that can and does not, or fails to, does not hold it."""
    try:
        from kiln.printers.base import PrinterAdapter

        reader = getattr(type(adapter), "read_print_file", None)
        if reader is None or reader is PrinterAdapter.read_print_file:
            return True
        key = (_machine(adapter), _name(file_name), sent)
        now = time.monotonic()
        with _lock:
            seen = _read_back.get(key)
        if seen is not None and now - seen[0] < _READ_BACK_SECONDS:
            return seen[1]
        try:
            data = adapter.read_print_file(file_name)
        except Exception:  # noqa: BLE001 -- not read back is not held
            logger.debug("resume record: read-back of %s failed", file_name, exc_info=True)
            data = None
        held = bool(data) and hashlib.sha256(data).hexdigest() == sent
        with _lock:
            _read_back[key] = (now, held)
        return held
    except Exception:  # noqa: BLE001
        return False


def made_for_paused_job(file_name: str, *, adapter: Any) -> ResumeRecord | None:
    """The record that makes starting *file_name* on *adapter*'s machine a
    continuation of the job it reports now, or ``None``.

    The file must be the printer-side name a recorded file was uploaded
    under through Kiln, with nothing saved over that name since
    (:func:`note_upload`); a local copy that hashes the same is not the file
    the printer starts.  A printer that can hand a file back must still hold
    the bytes Kiln sent under that name.  The record must be recent, and the
    printer must report the same job now that it reported when the file was
    made.  Whether the printer is paused is asked by
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
        sent = record.uploaded_as.get(name)
        if sent and record.job == job_now and _printer_still_holds(adapter, file_name, sent):
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
        _read_back.clear()
        _sent.clear()
