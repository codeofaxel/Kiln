"""A resume file Kiln made, for tests of the doors that continue a paused print.

A start continues a paused print only when the file is one Kiln made for the
job the printer holds (:func:`kiln.print_signoff.verified_resume`).  Tests of
those doors make one here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from kiln import resume_provenance


def made_by_kiln(adapter: Any, file_name: str, *, made_by: str = "decorate_during_print") -> str:
    """Record a resume Kiln made for *adapter*'s machine and its current job,
    saved there under *file_name*'s base name.  When *file_name* is a file on
    this computer, its own bytes are the ones recorded.  A test printer that
    reports no job is given one, the job the resume continues.  Returns
    *file_name*."""
    if not resume_provenance.paused_job_name(adapter):
        from kiln.printers.base import JobProgress

        adapter.get_job = lambda: JobProgress(file_name="the_paused_job.gcode")
    if os.path.isfile(file_name):
        path = file_name
    else:
        fd, path = tempfile.mkstemp(prefix="kiln-resume-", suffix=Path(file_name).suffix or ".3mf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(f"a resume Kiln made: {file_name}".encode())
    record = resume_provenance.record_made_resume(path, adapter=adapter, made_by=made_by)
    assert record is not None, "the record was not made"
    resume_provenance.note_upload(adapter, os.path.basename(file_name), digest=record.sha256)
    return file_name
