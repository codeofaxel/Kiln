"""Whether a failure can go differently next time, read from what was raised.

Every error envelope Kiln returns carries ``retryable``.  An agent reads it to
decide whether to call again, so a wrong ``True`` sends it round a loop and a
wrong ``False`` makes it give up on a printer that was only busy.  The code a
door passes says little: most doors wrap whatever went wrong as the generic
``ERROR`` or ``INTERNAL_ERROR``, and both used to mean "retry", so a missing
file, a file that is not a model and a bad argument all told the agent to try
the same call again.

What was raised says more, and it is in hand wherever an envelope is built
inside an ``except`` block, which is where nearly all of them are built.  This
module is the one reading of it, asked by :func:`kiln.server._error_dict`:

* :func:`retryable` -- ``True`` for a failure that depends on something outside
  the call (the network, a timeout, a busy device), ``False`` for one that the
  same input reproduces (no such file, a file that is not a model, a value the
  call cannot use), ``None`` when the exception says neither, so the code's own
  default stands.
* :func:`refusal_code` -- the code a caller's input failure is answered with
  when a door labelled it ``INTERNAL_ERROR``: it was not Kiln's fault, and an
  agent that reads ``INTERNAL_ERROR`` waits for a fix that is not coming.

A cause the exception names (``raise ... from``, a ``PrinterError``'s
``cause``) is followed when the outer exception says neither, so an adapter
that re-wraps a timeout is still read as a timeout.

:func:`in_words` says the same reading to a person: what went wrong, as a
clause in everyday words, for a failure whose own text is Python's.
"""

from __future__ import annotations

import errno
import re
import struct
import zipfile
from collections.abc import Iterator

#: OS error numbers that describe the moment, not the input: try again later.
_TRANSIENT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EAGAIN", "EBUSY", "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET", "ECONNABORTED",
        "ENETUNREACH", "ENETDOWN", "EHOSTUNREACH", "EHOSTDOWN", "EPIPE", "EINTR",
    )
    if hasattr(errno, name)
)

#: Libraries whose own exceptions are about a network conversation.
_NETWORK_LIBRARIES = ("requests", "httpx", "httpcore", "urllib3", "aiohttp", "paho", "websocket")

#: Named by what the file system said: the path the caller gave is the problem.
_NOT_THERE: tuple[type[BaseException], ...] = (FileNotFoundError, NotADirectoryError, IsADirectoryError)
_PATH_ERRORS: tuple[type[BaseException], ...] = (*_NOT_THERE, PermissionError, FileExistsError)

#: Raised for a value the call cannot use.  The same value fails the same way.
_VALUE_ERRORS: tuple[type[BaseException], ...] = (
    ValueError, TypeError, zipfile.BadZipFile, struct.error,
)


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or getattr(exc, "cause", None)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, BlockingIOError, InterruptedError)):
        return True
    if type(exc).__name__ in ("TimeoutExpired", "Timeout", "ReadTimeout", "ConnectTimeout"):
        return True
    if isinstance(exc, OSError) and not isinstance(exc, _PATH_ERRORS) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    return any(
        (getattr(cls, "__module__", "") or "").split(".")[0] in _NETWORK_LIBRARIES
        for cls in type(exc).__mro__
    )


def _is_caller_input(exc: BaseException) -> bool:
    return isinstance(exc, _PATH_ERRORS + _VALUE_ERRORS)


def retryable(exc: BaseException | None) -> bool | None:
    """``True`` when *exc* says the failure may not happen again, ``False``
    when the same call will fail the same way, ``None`` when it says neither."""
    for link in _chain(exc):
        if _is_transient(link):
            return True
        if _is_caller_input(link):
            return False
    return None


def named_code(exc: BaseException | None) -> str | None:
    """The code an exception names for itself (a :class:`kiln.printers.PrinterError`
    raised with ``code=``), or ``None``.

    The sentence a person reads never carries the code; this is how an agent
    envelope or a ``--json`` reply still gets the specific one rather than
    the door's generic label.
    """
    from kiln.printers.base import PrinterError

    for link in _chain(exc):
        code = getattr(link, "code", None) if isinstance(link, PrinterError) else None
        if isinstance(code, str) and code:
            return code
    return None


def refusal_code(exc: BaseException | None) -> str | None:
    """The code for a caller's input failure, or ``None`` for anything else.

    Only the failures that are plainly about what the caller handed in: a path
    the file system refused, and a file Kiln's readers could not read as a
    model (:class:`kiln.mesh_errors.UnreadableMeshError`, which a mesh too
    large to hold and a path with no file at it are too).  A bare
    ``ValueError`` is left alone: it is not retryable, but it can as well be
    Kiln's own mistake.
    """
    from kiln.mesh_errors import MeshFileMissing, UnreadableMeshError

    for link in _chain(exc):
        if _is_transient(link):
            return None
        if isinstance(link, PermissionError):
            return "PERMISSION_DENIED"
        if isinstance(link, (*_NOT_THERE, MeshFileMissing)):
            return "FILE_NOT_FOUND"
        if isinstance(link, UnreadableMeshError):
            return "UNREADABLE_INPUT"
    return None


#: Raised by a mistake in Kiln's own code, never by what the person did.
_KILN_FAULTS: tuple[type[BaseException], ...] = (
    KeyError, IndexError, AttributeError, AssertionError, NameError, RecursionError, NotImplementedError,
    ArithmeticError,
)
#: Said for a failure nothing else here names, and for Kiln's own faults.
INSIDE_KILN = "something went wrong inside Kiln (not something you did)"


#: A web service's answer named in a failure's text ("HTTP 401", "status
#: 503", "404 Client Error"), by what it means for the person.
_HTTP_STATUS = re.compile(r"\b(?:HTTP|status)[ :]*([1-5]\d\d)\b|\b([45]\d\d) (?:Client|Server) Error\b", re.IGNORECASE)


def _status_in_words(code: int) -> str | None:
    if code in (401, 403):
        return "the other service didn't accept Kiln's sign-in"
    if code == 404:
        return "what was asked for wasn't found"
    if code in (408, 504):
        return "it took too long to answer"
    if code == 429:
        return "too many requests went out at once; try again shortly"
    if 400 <= code < 500:
        return "the other service didn't accept the request"
    if 500 <= code < 600:
        return "the other service had a problem on its side"
    return None


def _one_in_words(exc: BaseException) -> str | None:
    name = type(exc).__name__
    status = _HTTP_STATUS.search(str(exc))
    if status is not None:
        said = _status_in_words(int(status.group(1) or status.group(2)))
        if said is not None:
            return said
    if isinstance(exc, FileNotFoundError):
        return "the file wasn't found"
    if isinstance(exc, (NotADirectoryError, IsADirectoryError)):
        return "that path isn't a file"
    if isinstance(exc, PermissionError):
        return "Kiln isn't allowed to open that file or folder"
    if isinstance(exc, FileExistsError):
        return "a file with that name is already there"
    if isinstance(exc, TimeoutError) or name in ("TimeoutExpired", "Timeout", "ReadTimeout", "ConnectTimeout"):
        return "it took too long to answer"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "the disk is full"
    if _is_transient(exc):
        return "the connection didn't go through"
    if isinstance(exc, (zipfile.BadZipFile, struct.error)):
        return "the file is damaged, or isn't the kind of file its name says"
    if name == "JSONDecodeError" or isinstance(exc, UnicodeError):
        return "the file or reply couldn't be read"
    if isinstance(exc, MemoryError):
        return "it needs more memory than this computer has free"
    if isinstance(exc, ImportError):
        return "a piece Kiln needs isn't installed"
    if name == "CalledProcessError":
        return "a program Kiln runs stopped with an error"
    if type(exc) in _KILN_FAULTS:
        return INSIDE_KILN
    return None


def in_words(exc: BaseException | None) -> str:
    """What went wrong, as a clause a person reads ("the file wasn't
    found"), from the exception or the first cause that says."""
    for link in _chain(exc):
        said = _one_in_words(link)
        if said is not None:
            return said
    if isinstance(exc, _VALUE_ERRORS):
        return "a value it was given can't be used"
    return INSIDE_KILN
