"""Whether the bytes that will print are the bytes a person's yes covers.

A start finds its yes by the file's name.  This module holds it to the
file's bytes.  The yes and the preview each record the sha256 of what they
were about (:class:`kiln.print_signoff.Clearance`).  Right before a print
starts, the bytes that will print are named (:func:`what_prints`), and each
recorded fact must cover them (:func:`covers`): the same bytes, or a file
Kiln made from those bytes (a slice, its wrap, or a copy that only moved the
part on the plate), joined by bytes through the slice ledger, never by name.

What will print, in order: the printer's own copy when the gate has just
read it back; the bytes Kiln itself sent under that name, while the printer
reports nothing that disagrees; the printer's copy read back now.  When none
of those can be had, the bytes are unknown, and a yes given for particular
bytes does not start them.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: How the bytes that will print were named.
PRINTER_COPY = "printer_copy"
KILN_SENT = "kiln_sent"
UNKNOWN = "unknown"

#: Refusal codes, carried on the start's result.
CODE_NOT_APPROVED = "PRINT_FILE_NOT_APPROVED"
CODE_UNCHECKED = "PRINT_FILE_UNCHECKED"

#: The shortest digest compared: the preview token keeps the first 32 hex
#: characters of a sha256, every other record keeps all 64.
_MIN_DIGEST = 32

#: Arguments a door may name the local file behind a start with.
_LOCAL_KEYS = ("source_path", "local_path", "local_file_path", "gcode_path", "file_path", "threemf_path")


def _sha(path: Any) -> str | None:
    from kiln.resume_provenance import file_sha256

    return file_sha256(path)


def _named(digest: Any) -> bool:
    text = str(digest or "")
    return len(text) >= _MIN_DIGEST and not text.startswith("NO_FILE")


def same(a: Any, b: Any) -> bool:
    """Whether two digests name the same bytes (a short one is a prefix)."""
    if not _named(a) or not _named(b):
        return False
    x, y = str(a).lower(), str(b).lower()
    n = min(len(x), len(y))
    return x[:n] == y[:n]


def _local_files(file_name: str, kwargs: dict[str, Any] | None) -> list[str]:
    """Local files a start names for itself: a path given as the name, and
    the paths a door passed beside it.  Only ever compared by bytes."""
    found: list[str] = []
    for value in [file_name, *((kwargs or {}).get(k) for k in _LOCAL_KEYS)]:
        if isinstance(value, str) and value and os.path.isfile(value) and value not in found:
            found.append(value)
    return found


def _printer_reports_other_bytes(adapter: Any, file_name: str, sent: str, kwargs: dict[str, Any] | None) -> bool:
    """Whether the printer reports a size for *file_name* other than the
    size of the bytes Kiln sent under it, when a local file of those bytes
    is at hand to measure.  A printer that reports nothing is taken at
    Kiln's record."""
    try:
        found = adapter.print_file_identity(file_name)
    except Exception:  # noqa: BLE001 -- a printer that cannot say has said nothing
        return False
    size = found[0] if found else None
    if not isinstance(size, int) or isinstance(size, bool):
        return False
    for local in _local_files(file_name, kwargs):
        if same(_sha(local), sent):
            try:
                return os.path.getsize(local) != size
            except OSError:
                return False
    return False


def what_prints(
    adapter: Any, file_name: str, kwargs: dict[str, Any] | None = None, *, printed: str | None = None,
) -> tuple[str | None, str]:
    """``(sha256, how)`` of the bytes this start would print, or
    ``(None, UNKNOWN)``.  *printed* is the printer's copy the gate already
    read back for this start, when it did."""
    if printed and os.path.isfile(printed) and (digest := _sha(printed)):
        return digest, PRINTER_COPY
    from kiln.resume_provenance import sent_sha256

    sent = sent_sha256(adapter, file_name)
    if sent and not _printer_reports_other_bytes(adapter, file_name, sent, kwargs):
        return sent, KILN_SENT
    from kiln.printer_copy import copy_from_printer

    copy = copy_from_printer(adapter, file_name)
    if copy.path and (digest := _sha(copy.path)):
        return digest, PRINTER_COPY
    return None, UNKNOWN


def _made_from(file_name: str, printed_sha: str, kwargs: dict[str, Any] | None) -> list[str]:
    """The local files the printed bytes were made from, by the slice
    ledger, for the row whose G-code or wrap IS the printed bytes.  Empty
    when Kiln made no such file."""
    from kiln.monitor_twin import sliced_entry_for

    names = [os.path.basename(str(file_name or ""))]
    for local in _local_files(file_name, kwargs):
        if same(_sha(local), printed_sha):
            names.append(os.path.basename(local))
    for name in dict.fromkeys(n for n in names if n):
        entry = sliced_entry_for(name)
        if not isinstance(entry, dict):
            continue
        output = str(entry.get("output") or "")
        wrapped = str(entry.get("wrapped") or "")
        made = [p for p in (output, wrapped) if p and os.path.basename(p) == name]
        if not any(same(_sha(p), printed_sha) for p in made if os.path.isfile(p)):
            continue
        sources = [str(entry.get("input") or ""), str(entry.get("source") or "")]
        if wrapped and os.path.basename(wrapped) == name and output:
            sources.append(output)
        return [s for s in sources if s and os.path.isfile(s)]
    return []


def covers(fact_sha: str, printed_sha: str, file_name: str, kwargs: dict[str, Any] | None = None) -> bool:
    """Whether a yes or preview given for the bytes *fact_sha* covers a
    start that would print the bytes *printed_sha*."""
    if same(fact_sha, printed_sha):
        return True
    from kiln.print_signoff import approved_part

    for source in _made_from(file_name, printed_sha, kwargs):
        if same(fact_sha, _sha(source)):
            return True
        part = approved_part(source)
        if part and part != source and same(fact_sha, _sha(part)):
            return True
    return False


def refusal(
    adapter: Any, file_name: str, kwargs: dict[str, Any] | None, clearance: Any, *, printed: str | None = None,
) -> dict[str, str] | None:
    """The refusal for a start whose bytes the yes or the preview does not
    cover, or ``None`` to allow.  A clearance that names no bytes (the CI
    switch, a resume, a stored record from before bytes were kept) is not
    judged here."""
    facts = [
        str(digest) for digest in (getattr(clearance, "yes_sha256", ""), getattr(clearance, "preview_sha256", ""))
        if _named(digest)
    ]
    if not facts:
        return None
    name = os.path.basename(str(file_name or "")) or "this file"
    printed_sha, how = what_prints(adapter, file_name, kwargs, printed=printed)
    if printed_sha is None:
        return {
            "reason": (
                f"{name} was not started: Kiln can't read {name} back from this printer to check that it is "
                "the file that was approved, so nothing moved. Start it from the file on this computer, so Kiln "
                "sends exactly the file that was approved."
            ),
            "code": CODE_UNCHECKED,
        }
    if all(covers(fact, printed_sha, file_name, kwargs) for fact in facts):
        return None
    logger.info("approved bytes: %s on the printer is not the approved file (%s)", name, how)
    return {
        "reason": (
            f"{name} was not started: the {name} on the printer is not the file that was approved, so nothing "
            "was sent and nothing moved. Start the approved file from this computer, or show the file that is "
            "on the printer and ask again."
        ),
        "code": CODE_NOT_APPROVED,
    }
