"""A resume file Kiln made, for tests of the doors that continue a paused print.

A start continues a paused print only when the file is one Kiln made for the
job the printer holds, from Kiln's own copy of that job's G-code
(:func:`kiln.print_signoff.verified_resume`,
:func:`kiln.resume_provenance.try_record_resume`).  Tests of those doors make
one here: :func:`kiln_started` writes Kiln's record of the print the printer
is running, and :func:`made_by_kiln` makes a resume from it.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from kiln import monitor_twin, resume_provenance
from kiln.registry import machine_fingerprint

#: A start stamp no real print carries, so a test record never reads as the
#: newest print start on this machine.
_LONG_AGO = "2000-01-01T00:00:00+00:00"


def kiln_started(adapter: Any, job: str, toolpath: bytes = b"G28\nG1 Z0.2\n; the print Kiln started\n") -> str:
    """Kiln's own record of the print *adapter* is running: the job by its
    printer-side name, and the toolpath Kiln kept, with its SHA-256.  Returns
    the kept toolpath's path, the only source a resume of it may be built
    from."""
    fd, path = tempfile.mkstemp(prefix="kiln-job-", suffix=".gcode")
    with os.fdopen(fd, "wb") as fh:
        fh.write(toolpath)
    active = monitor_twin._read_json(monitor_twin._ACTIVE_FILE, {})
    if not isinstance(active, dict):
        active = {}
    active[f"resume-test-{id(adapter):x}"] = {
        "file_name": os.path.basename(job),
        "printer_name": "resume-test",
        "machine": machine_fingerprint(adapter),
        "started_at": _LONG_AGO,
        "gcode": path,
        "gcode_sha256": hashlib.sha256(toolpath).hexdigest(),
        "mesh": None,
    }
    monitor_twin._TWIN_DIR.mkdir(parents=True, exist_ok=True)
    monitor_twin._write_json(monitor_twin._ACTIVE_FILE, active)
    return path


def made_by_kiln(adapter: Any, file_name: str, *, made_by: str = "decorate_during_print") -> str:
    """Record a resume Kiln made for *adapter*'s machine and its current job,
    from Kiln's own copy of that job, saved there under *file_name*'s base
    name.  When *file_name* is a file on this computer, its own bytes are the
    ones recorded.  A test printer that reports no job is given one, the job
    the resume continues.  Returns *file_name*."""
    if not resume_provenance.paused_job_name(adapter):
        from kiln.printers.base import JobProgress

        adapter.get_job = lambda: JobProgress(file_name="the_paused_job.gcode")
    source = kiln_started(adapter, resume_provenance.paused_job_name(adapter))
    if os.path.isfile(file_name):
        path = file_name
    else:
        fd, path = tempfile.mkstemp(prefix="kiln-resume-", suffix=Path(file_name).suffix or ".3mf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(f"a resume Kiln made: {file_name}".encode())
    record, why = resume_provenance.try_record_resume(path, source=source, adapter=adapter, made_by=made_by)
    assert record is not None, f"the record was not made: {why}"
    resume_provenance.note_upload(adapter, os.path.basename(file_name), digest=record.sha256)
    return file_name


def reset_resume_records() -> None:
    """Forget every resume record and what Kiln knows each printer-side name
    holds, as a new process would."""
    with resume_provenance._lock:
        resume_provenance._records.clear()
        resume_provenance._reported.clear()
        resume_provenance._read_back.clear()
        resume_provenance._sent.clear()
