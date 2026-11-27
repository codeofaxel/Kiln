"""The printer's own copy of a file this computer holds no copy of.

A start can name a file only by its name on the printer: a file Kiln sent
before it was restarted, or one another program sent.  To put that print to
the person anywhere but this computer's screen, Kiln needs the bytes that
will print, so it reads them back from the printer into a private folder
here.  A printer that cannot hand a file back says so, and nothing is read.

A copy is kept so a large file is read once, not once per start.  It is
used again only when the printer reports the same size AND the same time
stamp for the file under the same name on the same machine; a file whose
time stamp the printer does not report is read again every time, because a
changed setting can leave a file the same size.  Copies are named by the
sha256 of their own bytes, so a copy can never stand for other bytes.

Never raises.  Off the hosted server only: callers there never reach it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: How each answer came about.
READ_BACK = "read_back"
KEPT = "kept_copy"
CANNOT_READ_BACK = "cannot_read_back"
READ_FAILED = "read_failed"

#: Where the copies live, inside :func:`kiln.output_paths.kiln_output_dir`.
_FOLDER = "kiln-printer-copies"
_INDEX = "index.json"

#: The most copies kept at once; the oldest go first.
_MAX_COPIES = 8

#: A time stamp before this reads as a clock that was never set, which
#: cannot tell one upload from the next.
_EARLIEST_CLOCK = 946_684_800


@dataclass(frozen=True)
class PrinterCopy:
    """A local file holding the printer's bytes (``path``), or ``None`` with
    the reason in ``how`` and, for a read that failed, what went wrong in
    words (``why``)."""

    path: str | None
    how: str
    why: str = ""


def can_read_back(adapter: Any) -> bool:
    """Whether *adapter*'s printer can hand a file back at all: it has a
    reader of its own, not the base class's, which hands nothing back."""
    from kiln.printers.base import PrinterAdapter

    reader = getattr(adapter, "read_print_file", None)
    return callable(reader) and getattr(reader, "__func__", None) is not PrinterAdapter.read_print_file


def _suffix(file_name: str) -> str:
    low = os.path.basename(str(file_name)).lower()
    if low.endswith(".gcode.3mf"):
        return ".gcode.3mf"
    return os.path.splitext(low)[1] or ".gcode"


def _identity(adapter: Any, file_name: str) -> tuple[int, int] | None:
    """``(size, time stamp)`` the printer reports for *file_name*, or
    ``None`` when it does not report both."""
    asker = getattr(adapter, "print_file_identity", None)
    if not callable(asker):
        return None
    try:
        found = asker(file_name)
    except Exception:  # noqa: BLE001 -- unknown identity is no identity
        logger.debug("printer copy: identity of %s not read", file_name, exc_info=True)
        return None
    if not found:
        return None
    size, stamp = found
    if not isinstance(size, int) or size <= 0 or not isinstance(stamp, int) or stamp < _EARLIEST_CLOCK:
        return None
    return size, stamp


def _key(adapter: Any, file_name: str, identity: tuple[int, int]) -> str:
    from kiln.registry import machine_fingerprint

    machine = machine_fingerprint(adapter)
    # The whole name, folders included: two folders can hold files of one
    # name, size and time.
    name = str(file_name).replace("\\", "/").lstrip("/")
    size, stamp = identity
    return hashlib.sha256(f"{machine}\0{name}\0{size}\0{stamp}".encode()).hexdigest()


def _folder() -> str:
    from kiln.output_paths import kiln_output_dir

    return kiln_output_dir(_FOLDER)


def _read_index(folder: str) -> dict[str, str]:
    try:
        with open(os.path.join(folder, _INDEX), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomically(folder: str, name: str, data: bytes) -> str:
    fd, tmp = tempfile.mkstemp(prefix=".kiln-copy-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        path = os.path.join(folder, name)
        os.replace(tmp, path)
        return path
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _kept(folder: str, key: str, size: int, suffix: str) -> str | None:
    """The copy kept under *key*, when its file is still there and its size
    is the one the printer reports."""
    digest = _read_index(folder).get(key)
    if not digest:
        return None
    path = os.path.join(folder, digest + suffix)
    try:
        if os.path.getsize(path) != size:
            return None
    except OSError:
        return None
    return path


def _keep(folder: str, key: str | None, data: bytes, suffix: str) -> str:
    """Write *data* under the sha256 of its bytes; record it under *key*
    when there is one; drop the oldest copies past :data:`_MAX_COPIES`."""
    digest = hashlib.sha256(data).hexdigest()
    path = os.path.join(folder, digest + suffix)
    if not os.path.isfile(path):
        path = _write_atomically(folder, digest + suffix, data)
    else:
        with contextlib.suppress(OSError):
            os.utime(path)  # most recently used: kept longest
    with contextlib.suppress(Exception):
        index = _read_index(folder)
        if key:
            index[key] = digest
        copies = sorted(
            (e for e in os.scandir(folder) if e.is_file() and not e.name.startswith(".") and e.name != _INDEX),
            key=lambda e: e.stat().st_mtime,
        )
        for old in copies[:-_MAX_COPIES]:
            with contextlib.suppress(OSError):
                os.unlink(old.path)
        present = {e.name.split(".", 1)[0] for e in os.scandir(folder) if e.is_file()}
        index = {k: v for k, v in index.items() if v in present}
        _write_atomically(folder, _INDEX, json.dumps(index).encode())
    return path


def copy_from_printer(adapter: Any, file_name: str) -> PrinterCopy:
    """The printer's own bytes of *file_name*, as a local file.  Never raises."""
    try:
        if not can_read_back(adapter):
            return PrinterCopy(None, CANNOT_READ_BACK)
        suffix = _suffix(file_name)
        folder = _folder()
        identity = _identity(adapter, file_name)
        key = _key(adapter, file_name, identity) if identity else None
        if key and identity and (kept := _kept(folder, key, identity[0], suffix)):
            return PrinterCopy(kept, KEPT)
        started = time.monotonic()
        try:
            data = adapter.read_print_file(file_name)
        except Exception as exc:  # noqa: BLE001 -- a read that failed is no copy
            from kiln import plain_words

            logger.debug("printer copy: %s not read back", file_name, exc_info=True)
            return PrinterCopy(None, READ_FAILED, plain_words.said(exc))
        if data is None:
            return PrinterCopy(None, CANNOT_READ_BACK)
        if not data:
            return PrinterCopy(None, READ_FAILED, "the printer returned an empty file")
        # A file that changed while it was read is used for this start only.
        if identity and len(data) != identity[0]:
            key = None
        path = _keep(folder, key, data, suffix)
        logger.debug("printer copy: read %s back (%d bytes, %.1fs)", file_name, len(data), time.monotonic() - started)
        return PrinterCopy(path, READ_BACK)
    except Exception:  # noqa: BLE001
        logger.debug("printer copy: %s not copied", file_name, exc_info=True)
        return PrinterCopy(None, READ_FAILED)
