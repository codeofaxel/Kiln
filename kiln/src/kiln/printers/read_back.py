"""Reading a print file back from a printer: one contract, ceiling and wording.

``read_print_file(name)`` gives the bytes ``start_print`` would print under
*name*, ``None`` when this printer cannot hand that file back, and raises
:func:`read_failed` when it failed this time; ``print_file_identity(name)``
gives ``(size, Unix time)`` without sending it, either part ``None``."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from kiln.printers.base import PrinterError
from kiln.quoted_text import fence

#: The largest file any backend reads back.  A print file past it is too
#: large to check, and the refusal says so.
READ_BACK_CAP_BYTES: int = 256 * 1024 * 1024

#: Backends that never read a file back, by adapter class name, with why
#: the bytes that print are known without reading them.
NO_READ_BACK: dict[str, str] = {
    "SerialPrinterAdapter": (
        "a USB printer prints from its own SD card and has no command that sends a stored file back; "
        "Kiln knows the bytes only of a file it sent there itself"
    ),
}


def too_big(file_name: str, cap: int | None = None) -> PrinterError:
    """The failure for a file past the read-back ceiling."""
    cap = READ_BACK_CAP_BYTES if cap is None else cap
    return PrinterError(
        f"{fence(file_name)} is larger than the {cap // (1024 * 1024)} MB Kiln reads back from a printer, "
        "so Kiln could not check it. Start it from the file on this computer, or from the "
        "printer's own screen.",
        code="PRINT_FILE_TOO_LARGE",
    )


def read_failed(file_name: str, exc: BaseException | str) -> PrinterError:
    """The failure for a read-back that did not come through this time."""
    from kiln import plain_words

    why = (plain_words.said(exc) if isinstance(exc, BaseException) else str(exc)).strip().rstrip(".")
    return PrinterError(
        f"Kiln couldn't read {fence(file_name)} back from the printer: {why}. "
        "Use `printer_files` to check the file is there.",
        cause=exc if isinstance(exc, Exception) else None,
        code="PRINT_FILE_READ_FAILED",
    )


def collect_capped(chunks: Iterable[bytes], file_name: str, cap: int | None = None) -> bytes:
    """Join *chunks*, refusing (:func:`too_big`) once they pass *cap*
    (the module's ceiling, read when called, unless one is given)."""
    cap = READ_BACK_CAP_BYTES if cap is None else cap
    parts: list[bytes] = []
    size = 0
    for chunk in chunks:
        if not chunk:
            continue
        size += len(chunk)
        if size > cap:
            raise too_big(file_name, cap)
        parts.append(chunk)
    return b"".join(parts)


def unix_time(value: object) -> int | None:
    """A Unix time stamp from what a printer reports: seconds or
    milliseconds since 1970, an ISO 8601 string, or an HTTP date.  ``None``
    for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return int(seconds) if seconds > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return unix_time(float(text))
    except ValueError:
        pass
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return int(when.timestamp())
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, IndexError):
        return None
